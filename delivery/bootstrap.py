"""Provision only a freshly created CI stack. Never run against staging/production."""
import contextlib
import io
import json
import os
from pathlib import Path
import secrets
import requests
from .wait import until

RUNTIME = Path("/run/nda")


def require_ci():
    marker = RUNTIME / "ci-marker.json"
    if not marker.exists() or not json.loads(marker.read_text())["project"].startswith("nda-ci-"):
        raise RuntimeError("Synthetic provisioning requires an isolated CI marker")
    if os.environ.get("KEYCLOAK_URL") != "http://nda-keycloak:8180":
        raise RuntimeError("CI bootstrap may only use its private Compose network")


def identities():
    require_ci()
    import hcl2
    from marketplace.identity import token
    base = "http://nda-keycloak:8180"
    until("Keycloak", lambda: requests.get(base + "/realms/master", timeout=5).status_code == 200)
    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + token()

    def post(path, data):
        r = session.post(base + "/admin/realms" + path, json=data, timeout=30)
        r.raise_for_status()
        return r

    with open("infra/variables.tf", encoding="utf-8") as f:
        variables = {k: v for block in hcl2.load(f)["variable"] for k, v in block.items()}
    post("", {"realm": "nda", "enabled": True, "sslRequired": "none"})
    group_ids, entitlements = {}, {}
    for name, spec in variables["stakeholder_groups"]["default"].items():
        r = post("/nda/groups", {"name": name, "attributes": {"trino_role": [spec["trino_role"]]}})
        group_ids[name] = r.headers["Location"].rsplit("/", 1)[1]
        entitlements[name] = {
            key: spec[key] for key in ("description", "processes", "layers", "indicators",
                                       "formats", "full_dashboard", "row_limit", "trino_role")}
    credentials = {"users": {}}
    for user, spec in variables["stakeholder_users"]["default"].items():
        password = secrets.token_urlsafe(24)
        r = post("/nda/users", {"username": user, "enabled": True, "emailVerified": True,
            "firstName": spec["first_name"], "lastName": spec["last_name"], "email": spec["email"],
            "credentials": [{"type": "password", "value": password, "temporary": False}]})
        uid = r.headers["Location"].rsplit("/", 1)[1]
        session.put(base + f"/admin/realms/nda/users/{uid}/groups/{group_ids[spec['group']]}", timeout=30).raise_for_status()
        credentials["users"][user] = {"password": password, "group": spec["group"]}
    secret = secrets.token_urlsafe(32)
    post("/nda/clients", {"clientId": "trino", "secret": secret, "publicClient": False,
        "standardFlowEnabled": True, "directAccessGrantsEnabled": True,
        "redirectUris": ["https://trino-marketplace:8443/oauth2/callback"],
        "protocolMappers": [{"name": "trino-audience", "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper", "config": {
                "included.client.audience": "trino", "access.token.claim": "true", "id.token.claim": "false"}}]})
    issuer = base + "/realms/nda"
    protocol = issuer + "/protocol/openid-connect"
    settings = {"issuer": issuer, "client_id": "trino", "client_secret": secret,
                "auth_url": protocol + "/auth", "token_url": protocol + "/token",
                "internal_token_url": protocol + "/token", "internal_jwks_url": protocol + "/certs"}
    (RUNTIME / "trino_oidc.json").write_text(json.dumps(settings), encoding="utf-8")
    (RUNTIME / "credentials.json").write_text(json.dumps(credentials), encoding="utf-8")
    # OPA is a separate process: it needs the same entitlement declaration as
    # Keycloak before API authorization can be tested. Terraform renders this on
    # the VM; CI renders the equivalent disposable bundle here.
    policy = RUNTIME / "policy"
    policy.mkdir(parents=True, exist_ok=True)
    (policy / "authz.rego").write_text(Path("infra/policy/authz.rego").read_text(encoding="utf-8"),
                                        encoding="utf-8")
    (policy / "data.json").write_text(json.dumps({"entitlements": entitlements}), encoding="utf-8")
    from marketplace import tls, trino_config
    # The interactive developer command prints its password; CI never logs it.
    with contextlib.redirect_stdout(io.StringIO()):
        tls.issue()
    trino_config.write()
    print("CI Keycloak realm and authenticated Trino configuration provisioned")


def source():
    require_ci()
    from streaming import bootstrap
    import pymssql
    def ready():
        c = pymssql.connect(server="nda-sqlserver", user="sa", password=os.environ["MSSQL_SA_PASSWORD"], login_timeout=5)
        c.close()
        return True
    until("SQL Server", ready)
    bootstrap.source()
    until("Kafka Connect", lambda: requests.get("http://connect:8083/connectors", timeout=5).status_code == 200)
    bootstrap.connector()
    import boto3
    s3 = boto3.client("s3", endpoint_url="http://minio:9000", region_name="us-east-1",
                      aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"])
    def bucket():
        if "warehouse" not in [b["Name"] for b in s3.list_buckets()["Buckets"]]:
            s3.create_bucket(Bucket="warehouse")
        return True
    until("MinIO warehouse", bucket)
    until("Iceberg catalog", lambda: requests.get("http://iceberg-rest:8181/v1/config", timeout=5).status_code == 200)


if __name__ == "__main__":
    import sys
    {"identities": identities, "source": source}[sys.argv[1]]()
