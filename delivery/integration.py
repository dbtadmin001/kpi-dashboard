"""Mandatory real-engine assertions. No skips, fixtures substituted for services, or production writes."""
import json
import os
from pathlib import Path
import subprocess
import time
import requests
from .bootstrap import require_ci
from .wait import until


def query(user, sql):
    from marketplace.auth import connect
    from marketplace.build import run
    with connect(user) as c:
        return run(c, sql)


def denied(user, sql):
    from trino.exceptions import TrinoUserError
    try:
        query(user, sql)
    except TrinoUserError as exc:
        if exc.error_name == "PERMISSION_DENIED":
            return
        raise
    raise AssertionError(f"Unexpected grant for {user}")


def expected_events():
    from streaming.simulator import lifecycles
    return list(lifecycles(count=3, seed=42))


def ingest():
    require_ci()
    from streaming.simulator import connect, write_state
    with connect() as c:
        for table, row in expected_events():
            write_state(c, table, row)
    print("Committed deterministic source transactions")


def reconcile():
    from streaming.contracts import COLUMNS, table_names
    from datetime import datetime
    state = {}
    for table, row in expected_events():
        state[table, row["record_id"]] = row
    columns = list(COLUMNS)
    for table in table_names():
        expected = []
        for (t, _), row in state.items():
            if t == table:
                expected.append(tuple(int((v - datetime(1970, 1, 1)).total_seconds() * 1000)
                                      if isinstance(v, datetime) else v for v in (row[k] for k in columns)))
        for schema, target in [("nda_silver", table), ("nda_gold", "fact_" + table)]:
            actual = query("marketplace_owner", f"SELECT {','.join(columns)} FROM iceberg.{schema}.{target}")
            assert sorted(map(tuple, actual)) == sorted(expected), f"{schema}.{target} differs from committed source"
        assert query("marketplace_owner", f"SELECT COUNT(*) FROM iceberg.nda_silver.quarantine_{table}")[0][0] == 0
    return True


def authorization():
    from marketplace.products import PRODUCTS, products_for
    roles = {"public.viewer": "business_user", "alice.nakato": "analyst",
             "sam.scientist": "data_scientist", "dana.okello": "data_engineer"}
    for user, role in roles.items():
        identity = query(user, "SELECT current_user, current_groups()")[0]
        assert identity[0] == user and role in identity[1]
        allowed = {p.name for p in products_for(role)}
        for product in PRODUCTS:
            sql = f"SELECT COUNT(*) FROM {product.fqn}"
            if product.name in allowed:
                query(user, sql)
            else:
                denied(user, sql)
    denied("public.viewer", "SELECT COUNT(*) FROM iceberg.nda_gold.fact_ma_applications")
    denied("alice.nakato", "SELECT COUNT(*) FROM iceberg.nda_bronze.ma_applications")
    # A disposable object in an isolated stack, never a production fact table.
    query("marketplace_owner", "CREATE TABLE iceberg.nda_gold.ci_write_probe (id INTEGER)")
    try:
        denied("alice.nakato", "DROP TABLE iceberg.nda_gold.ci_write_probe")
    finally:
        query("marketplace_owner", "DROP TABLE IF EXISTS iceberg.nda_gold.ci_write_probe")
    from marketplace.auth import access_token, ca_bundle
    response = requests.post("https://trino-marketplace:8443/v1/statement", data="SELECT 1",
                             verify=ca_bundle(), timeout=15)
    assert response.status_code == 401
    from trino.auth import JWTAuthentication
    import trino
    from trino.exceptions import TrinoUserError
    with trino.dbapi.connect(host="trino-marketplace", port=8443, user="dana.okello", http_scheme="https",
                            verify=ca_bundle(), auth=JWTAuthentication(access_token("alice.nakato"))) as c:
        try:
            c.cursor().execute("SELECT current_user").fetchall()
        except TrinoUserError as exc:
            assert exc.error_name == "PERMISSION_DENIED"
        else:
            raise AssertionError("Identity impersonation was accepted")


def sso():
    """Exercise authorization code + callback, independently of password-grant tokens."""
    import re
    from html.parser import HTMLParser
    from marketplace.auth import ca_bundle, password_for
    class Form(HTMLParser):
        action = None
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form" and attrs.get("id") == "kc-form-login":
                self.action = attrs["action"]
    for user in ("alice.nakato", "public.viewer"):
        session = requests.Session()
        session.verify = ca_bundle()
        url = "https://trino-marketplace:8443/v1/statement"
        response = session.post(url, data="SELECT 1", headers={"X-Trino-User": user}, timeout=15)
        assert response.status_code == 401
        header = response.headers["WWW-Authenticate"]
        redirect = re.search(r'x_redirect_server="([^"]+)"', header)[1]
        poll = re.search(r'x_token_server="([^"]+)"', header)[1]
        page = session.get(redirect, timeout=15)
        form = Form()
        form.feed(page.text)
        assert form.action, "Keycloak login form missing"
        response = session.post(form.action, data={"username": user, "password": password_for(user), "credentialId": ""}, timeout=30)
        assert response.status_code == 200 and "/oauth2/callback" in response.url
        token = session.get(poll, timeout=30).json()["token"]
        headers = {"X-Trino-User": user, "Authorization": "Bearer " + token}
        result = session.post(url, data="SELECT current_user", headers=headers, timeout=15).json()
        rows = []
        for _ in range(100):
            assert "error" not in result, "OAuth-authenticated query failed"
            rows += result.get("data", [])
            if "nextUri" not in result:
                break
            result = session.get(result["nextUri"], headers=headers, timeout=15).json()
        assert rows == [[user]]


def unit_tests():
    """The 59 assertions that pin the governance invariants - group-only grants,
    mask placement, fail-closed directory, impersonation denial. They were locked
    in requirements and copied into the image, and then never run."""
    subprocess.run(["python", "-m", "pytest", "-q", "streaming/tests"], check=True)


def semantic_layer():
    """dbt build validates the models; it never touches the metrics. A broken
    semantic model would otherwise ship green."""
    from marketplace.auth import access_token, ca_bundle
    env = {**os.environ, "TRINO_JWT": access_token("marketplace_owner"),
           "TRINO_USER": "marketplace_owner", "TRINO_CA": ca_bundle(),
           "TRINO_HOST": "trino-marketplace", "PYTHONIOENCODING": "utf-8"}
    listed = subprocess.run(["mf", "list", "metrics"], cwd="marketplace/dbt",
                            env=env, capture_output=True, text=True, check=True)
    from marketplace.products import PRODUCTS
    expected = sum(len(p.measures) for p in PRODUCTS)
    found = listed.stdout.count("•") or listed.stdout.count("- ")
    assert found >= expected, f"expected {expected} metrics, mf listed {found}"
    # And one that actually compiles to SQL and returns rows.
    result = subprocess.run(
        ["mf", "query", "--metrics", "delay_analysis_avg_waiting_days",
         "--group-by", "delay_analysis__workflow_stage"],
        cwd="marketplace/dbt", env=env, capture_output=True, text=True, check=True)
    assert "delay_analysis_avg_waiting_days" in result.stdout, result.stdout[:400]
    print(f"Semantic layer: {found} metrics listed, one query executed")


def audited():
    """The audit is a governance control, so it is a release gate like any other.
    It used to be the only control switched off for CI."""
    marker = "AUDIT-GATE-" + str(int(time.time()))
    query("alice.nakato", f"SELECT '{marker}', COUNT(*) FROM iceberg.marketplace.delay_analysis")

    def recorded():
        rows = query("marketplace_owner",
                     "SELECT username, principal FROM iceberg.marketplace_audit.query_log "
                     f"WHERE query_text LIKE '%{marker}%'")
        assert rows, "query not recorded"
        assert rows[0][0] == "alice.nakato", f"wrong user recorded: {rows[0]}"
        return True

    until("audit record", recorded, timeout=180)
    # The log names the PHYSICAL tables, not just the view the caller typed.
    touched = query("marketplace_owner",
                    "SELECT tables_accessed FROM iceberg.marketplace_audit.query_log "
                    f"WHERE query_text LIKE '%{marker}%'")[0][0]
    assert "nda_gold" in touched, f"audit did not resolve the view to its sources: {touched}"
    print("Audit: query recorded with its principal and its physical sources")


def main():
    require_ci()
    unit_tests()
    until("authenticated Trino", lambda: query("marketplace_owner", "SELECT 1"))
    until("source/silver/gold reconciliation", reconcile, timeout=600)
    from streaming.bootstrap import gold
    from marketplace.build import apply
    gold()
    apply()
    authorization()
    sso()
    # Replay every source event. Idempotence must hold after a committed checkpoint.
    ingest()
    until("idempotent replay", reconcile, timeout=180)
    from marketplace.auth import access_token, ca_bundle
    env = {**os.environ, "TRINO_JWT": access_token("marketplace_owner"),
           "TRINO_USER": "marketplace_owner", "TRINO_CA": ca_bundle(), "TRINO_HOST": "trino-marketplace"}
    subprocess.run(["dbt", "build", "--project-dir", "marketplace/dbt", "--profiles-dir", "marketplace/dbt"], env=env, check=True)
    semantic_layer()
    audited()
    report = {"status": "passed",
              "checks": ["unit-tests", "source-silver-gold", "quarantine", "idempotence",
                         "RBAC", "SSO", "dbt-build", "semantic-layer", "audit"],
              "timestamp": time.time()}
    (Path("/run/nda") / "integration.json").write_text(json.dumps(report, indent=2))
    print("Required integration gates passed")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["ingest"]:
        ingest()
    else:
        main()
