"""The read-only API.

Every route is a GET except one, and that one triggers a *read* of the providers.
There is no code path in this service that writes to any piece of infrastructure,
and `test_no_write_routes` fails the build if a mutating verb ever appears.

Authentication is deliberately optional in this first version: on a workstation
it runs open, and setting CATALOG_REQUIRE_AUTH=1 turns on OIDC against the same
Keycloak realm everything else uses. What is NOT optional is the audit log -
every request is recorded whether or not a principal was proven.
"""
import os
import pathlib
import time
import uuid

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import store
from .collectors import REGISTRY, all_collectors, health as collector_health
from .model import EDGE_TYPES
from .taxonomy import (EDGE_LABELS, GROUP_ABOUT, GROUP_LABEL, GROUPS,
                       confidence_label, label_for, source_for)
from .resolver import relink, run_collector

STATIC = pathlib.Path(__file__).parent / "static"
REQUIRE_AUTH = os.environ.get("CATALOG_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")

app = FastAPI(title="Infrastructure Catalog", version="0.1.0",
              description="Read-only catalog of Terraform, Kubernetes and platform resources.")
_conn = store.connect()


def principal(authorization: str = None) -> str:
    """Who is asking. Proven when auth is on, claimed when it is off - and the
    audit records which, so a log from an open instance is never mistaken for one
    from a secured deployment."""
    if not REQUIRE_AUTH:
        return "anonymous@open"
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Bearer token required")
    import base64
    import json
    try:
        body = authorization.split()[1].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return claims.get("preferred_username", "unknown")
    except Exception:                              # noqa: BLE001
        raise HTTPException(401, "Malformed token") from None


@app.middleware("http")
async def audit_every_request(request: Request, call_next):
    request_id = str(uuid.uuid4())[:8]
    started = time.perf_counter()
    response = await call_next(request)
    if request.url.path.startswith("/v1/"):
        store.record_audit(_conn, request.headers.get("x-principal", "anonymous@open"),
                           f"{request.method} {request.url.path}",
                           str(request.query_params)[:200], request_id)
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Elapsed-Ms"] = f"{(time.perf_counter()-started)*1000:.0f}"
    return response


# --------------------------------------------------------------------------
@app.get("/v1/stats")
def stats():
    data = store.stats(_conn)
    data["edge_types"] = EDGE_TYPES
    data["auth"] = "required" if REQUIRE_AUTH else "open"
    # The UI renders words, not identifiers. Ship the vocabulary with the data
    # so a new resource kind never shows up as a raw provider string.
    data["vocabulary"] = {
        "groups": [{"key": k, "label": l, "about": a} for k, l, a, _ in GROUPS],
        "edges": EDGE_LABELS,
        "sources": {p["value"]: source_for(p["value"]) for p in data["by_provider"]},
        "kinds": {k["value"]: label_for(k["value"]) for k in data["by_kind"]},
    }
    return data


@app.get("/v1/health")
def health():
    return {"collectors": collector_health(), "runs": store.last_runs(_conn)}


@app.get("/v1/resources")
def resources(q: str = None, kind: str = None, provider: str = None, scope: str = None,
              environment: str = None, owner: str = None, status: str = None,
              group: str = None, limit: int = Query(200, le=1000), offset: int = 0):
    return store.search(_conn, q, kind, provider, scope, environment, owner,
                        status, group, limit, offset)


@app.get("/v1/resources/{urn:path}")
def resource(urn: str):
    found = store.get_resource(_conn, urn)
    if not found:
        raise HTTPException(404, f"No resource {urn}")
    return found


@app.get("/v1/graph/neighbors")
def neighbors(urn: str, depth: int = Query(1, ge=1, le=4), types: str = None):
    return store.neighbors(_conn, urn, depth,
                           [t for t in (types or "").split(",") if t] or None)


@app.get("/v1/graph/impact")
def impact(urn: str, direction: str = "downstream", depth: int = Query(5, ge=1, le=6)):
    if direction not in ("downstream", "upstream"):
        raise HTTPException(400, "direction must be downstream or upstream")
    return store.impact(_conn, urn, direction, depth)


@app.get("/v1/access/principals")
def principals():
    """Users and groups, with the roles each ends up holding."""
    users = store.search(_conn, kind="user", limit=500)["items"]
    groups = store.search(_conn, kind="group", limit=200)["items"]
    return {"users": users, "groups": groups}


@app.get("/v1/access/effective")
def effective(principal: str):
    """Every capability a principal ends up with, and the path that produced it.

    Walked over the graph rather than recomputed, so the answer always matches
    what the catalog can show you.
    """
    matches = [r for r in store.search(_conn, q=principal, kind="user", limit=5)["items"]]
    if not matches:
        matches = store.search(_conn, q=principal, kind="group", limit=5)["items"]
    if not matches:
        raise HTTPException(404, f"No principal {principal}")
    start = matches[0]
    reach = store.impact(_conn, start["urn"], "downstream", 5)
    grants = [i for i in reach["items"] if i["kind"] in
              ("role", "data_product", "access_rule", "schema", "table", "view")]
    return {"principal": start["name"], "urn": start["urn"], "kind": start["kind"],
            "status": start["status"], "paths": grants}


@app.get("/v1/capacity")
def capacity():
    """What the estate is using. Honest about where the number came from."""
    volumes = store.search(_conn, kind="volume", limit=500)["items"]
    buckets = store.search(_conn, kind="bucket", limit=100)["items"]
    containers = store.search(_conn, kind="container", limit=500)["items"]
    running = [c for c in containers if c["status"] in ("running", "healthy")]
    return {
        "containers": {"total": len(containers), "running": len(running),
                       "unhealthy": len([c for c in containers
                                         if c["status"] not in ("running", "healthy")])},
        "volumes": {"count": len(volumes)},
        "buckets": [{"name": b["name"],
                     "objects": b["attributes"].get("objects_sampled"),
                     "bytes": b["attributes"].get("bytes_sampled")} for b in buckets],
        "restarts": sorted(
            [{"name": c["name"], "restarts": c["attributes"].get("restart_count", 0)}
             for c in containers if c["attributes"].get("restart_count", 0) > 0],
            key=lambda x: -x["restarts"])[:10],
        "note": "sampled from the Docker engine and MinIO; no metrics server on this estate",
    }


@app.get("/v1/audit")
def audit(limit: int = Query(200, le=1000)):
    return {"entries": store.audit(_conn, limit)}


@app.post("/v1/sync")
def sync(plugin: str = None):
    """The only non-GET route in the service, and it triggers a READ.

    Nothing here writes to any provider; it re-reads them and updates our own
    database. The name is a verb about this service, not about infrastructure.
    """
    names = [plugin] if plugin else None
    if plugin and plugin not in REGISTRY:
        raise HTTPException(404, f"No collector {plugin}")
    results = [run_collector(_conn, c, quiet=True) for c in all_collectors(names)]
    edges = relink(_conn)
    return {"ran": results, "relinked_edges": edges, "stats": store.stats(_conn)}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def serve(port=8600):
    import uvicorn
    print(f"Infrastructure Catalog on http://127.0.0.1:{port}")
    print(f"  auth: {'required' if REQUIRE_AUTH else 'OPEN (set CATALOG_REQUIRE_AUTH=1)'}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
