"""Authentication against Keycloak, authorization against OPA.

The split matters: Keycloak answers *who you are and which groups you are in*,
OPA answers *what that lets you do*. Neither is asked the other's question, so
changing an entitlement never requires touching identity, and vice versa. Both
are generated from one Terraform declaration (infra/), so they cannot drift.

Nothing in this module decides policy. Every decision is OPA's, fetched live, so
a Terraform apply takes effect without redeploying the API.
"""
import json
import os
import pathlib
import threading
import time
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
GENERATED = ROOT / "infra" / "generated" / "credentials.json"
_CACHE_TTL = 30
_cache = {}
_lock = threading.Lock()


class AuthError(Exception):
    """Credentials rejected, or a token that is no longer valid."""


def settings():
    """Client configuration, preferring the environment over the generated file."""
    data = {}
    if GENERATED.exists():
        data = json.loads(GENERATED.read_text(encoding="utf-8"))
    return {
        "url": os.environ.get("KEYCLOAK_URL", data.get("keycloak_url", "http://127.0.0.1:8180")).rstrip("/"),
        "realm": os.environ.get("KEYCLOAK_REALM", data.get("realm", "nda")),
        "client_id": os.environ.get("KEYCLOAK_CLIENT_ID", data.get("client_id", "nda-dashboard")),
        "client_secret": os.environ.get("KEYCLOAK_CLIENT_SECRET", data.get("client_secret", "")),
        "opa": os.environ.get("OPA_URL", "http://127.0.0.1:8182").rstrip("/"),
    }


def login(username, password):
    """Exchange credentials for a token. Returns (access_token, claims)."""
    config = settings()
    response = requests.post(
        f"{config['url']}/realms/{config['realm']}/protocol/openid-connect/token",
        data={"grant_type": "password", "username": username, "password": password,
              "client_id": config["client_id"], "client_secret": config["client_secret"],
              "scope": "openid profile email"},
        timeout=20)
    if response.status_code != 200:
        raise AuthError("Incorrect username or password")
    return response.json()["access_token"]


_jwks = {}


def _signing_keys(config):
    """Cache the realm's public keys; they rotate rarely."""
    from jwt import PyJWKClient
    url = f"{config['url']}/realms/{config['realm']}/protocol/openid-connect/certs"
    with _lock:
        client = _jwks.get(url)
        if client is None:
            client = _jwks[url] = PyJWKClient(url, cache_keys=True)
    return client


def claims(token):
    """Validate a token's signature, issuer and expiry, and return its claims.

    Verified locally against the realm's published keys rather than by calling
    Keycloak's introspection endpoint. That is the standard pattern, it costs no
    round trip per request, and it does not depend on introspection being
    enabled for the client. The cost is that a revoked session stays usable
    until the token expires, which is why the realm issues 10-minute tokens.
    """
    with _lock:
        hit = _cache.get(token)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL:
            return hit[1]
    import jwt
    config = settings()
    try:
        key = _signing_keys(config).get_signing_key_from_jwt(token)
        body = jwt.decode(token, key.key, algorithms=["RS256"],
                          issuer=f"{config['url']}/realms/{config['realm']}",
                          options={"verify_aud": False})
    except Exception as error:
        raise AuthError("Session is not valid; sign in again") from error
    if body.get("azp") and body["azp"] != config["client_id"]:
        raise AuthError("Token was not issued for this application")
    result = {"username": body.get("preferred_username"), "name": body.get("name"),
              "email": body.get("email"),
              "groups": [g.lstrip("/") for g in body.get("groups", [])]}
    with _lock:
        _cache[token] = (time.monotonic(), result)
    return result


def decide(groups, **payload):
    """Ask OPA. A policy engine that cannot be reached denies, never allows."""
    config = settings()
    try:
        response = requests.post(f"{config['opa']}/v1/data/nda/authz",
                                 json={"input": dict(groups=list(groups), **payload)}, timeout=10)
        response.raise_for_status()
    except requests.RequestException as error:
        raise AuthError("Authorization service unavailable") from error
    return response.json().get("result", {})


def entitlement(groups):
    """The caller's full entitlement, in one call."""
    result = decide(groups).get("entitlement")
    if not result:
        return {"groups": [], "processes": [], "layers": [], "formats": [],
                "indicators": [], "full_dashboard": False, "row_limit": 0, "can_export": False}
    return result


def allows(groups, **payload):
    return bool(decide(groups, **payload).get("allow"))


def visible_indicators(rights, process, available):
    """Filter one process's indicator keys down to what the caller may see."""
    if process not in rights.get("processes", []):
        return []
    allowed = rights.get("indicators")
    if allowed == "*":
        return list(available)
    return [key for key in available if key in set(allowed or [])]


def redact(payload, rights):
    """Strip a dashboard payload down to the caller's entitlement.

    Sections are emptied rather than removed, so the dashboard renders a
    consistent shape and shows "not available to you" instead of breaking.
    """
    processes = set(rights.get("processes", []))
    for section in ("quarterlyData", "kpiCounts", "processStepData", "bottleneckData", "processStepCounts"):
        block = payload.get(section)
        if not isinstance(block, dict):
            continue
        for process in list(block):
            if process not in processes:
                block[process] = {}
                continue
            if section in ("quarterlyData", "kpiCounts"):
                keep = set(visible_indicators(rights, process, list(block[process])))
                block[process] = {k: v for k, v in block[process].items() if k in keep}
    for section in ("quarterlyVolumes", "inspectionVolumes"):
        block = payload.get(section)
        if isinstance(block, dict):
            for process in list(block):
                if process not in processes:
                    block[process] = []
    # Volumes and process-step detail are operational, not published statistics.
    if not rights.get("full_dashboard") and rights.get("row_limit", 0) == 0:
        for section in ("quarterlyVolumes", "inspectionVolumes", "bottleneckData",
                        "processStepData", "processStepCounts"):
            if section in payload:
                payload[section] = {p: ({} if isinstance(payload[section].get(p), dict) else [])
                                    for p in payload[section]}
    payload.setdefault("_meta", {})["entitlement"] = {
        "groups": rights.get("groups", []), "processes": sorted(processes),
        "full_dashboard": rights.get("full_dashboard", False),
        "indicators": "all" if rights.get("indicators") == "*" else len(rights.get("indicators") or []),
    }
    return payload
