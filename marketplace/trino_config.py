"""A dedicated Trino coordinator for the marketplace, configured from the products.

Access control in Trino is engine-wide. Enabling rules on a cluster shared with
another project would govern that project's catalogs too, so the marketplace gets
its own coordinator pointed at the SAME Iceberg catalog and object store. No data
is copied; this is a second door with a lock on it.

    python -m marketplace.trino_config write     # render the config tree
    python -m marketplace.trino_config up        # start the coordinator
    python -m marketplace.trino_config check     # prove the rules actually bite
"""
import argparse
import json
import os
import pathlib
import subprocess
import time

from .build import OUT, access_rules
from .identity import DirectoryUnavailable, governed_users, group_file, role_members
from .products import MARKETPLACE_SCHEMA

CONTAINER = "trino-marketplace"
PORT = int(os.environ.get("MARKETPLACE_TRINO_PORT", "8090"))
NETWORK = os.environ.get("SHARED_NETWORK", "atc-poc_pipeline_net")
IMAGE = os.environ.get("TRINO_IMAGE", "trinodb/trino:455")
# The collector runs on the host; Docker Desktop resolves it by this name.
AUDIT_URI = os.environ.get("AUDIT_INGEST_URI",
                           "http://host.docker.internal:8099/v1/query-events")

NEWLINE = chr(10)
HTTPS_PORT = int(os.environ.get("MARKETPLACE_TRINO_HTTPS_PORT", "8443"))


def authentication_properties():
    """Turn the coordinator from 'tell me who you are' into 'prove it'.

    Two authentication types, tried in order. OAUTH2 sends a person's browser to
    Keycloak - the right flow for DBeaver and the CLI, because no password ever
    reaches the tool. JWT accepts a bearer token for programs that cannot open a
    browser. Both validate against the same realm and both take the Trino
    principal from `preferred_username`, so the two routes cannot disagree about
    who you are.

    Returns [] when the OIDC client has not been created yet, leaving the
    coordinator unauthenticated rather than unable to start - with a warning,
    because that state should be visible and temporary.
    """
    from .auth import OIDC_FILE, NotAuthenticated, oidc
    from .tls import KEYSTORE, internal_secret, keystore_password

    try:
        settings = oidc()
    except NotAuthenticated:
        if os.environ.get("MARKETPLACE_REQUIRE_AUTH") == "1":
            raise
        print(f"  WARNING: {OIDC_FILE.name} is missing, so the coordinator will accept")
        print("           any username without proof. Run ./infra/run.sh apply, then")
        print("           python -m marketplace.trino_config up")
        return []
    if not KEYSTORE.exists():
        raise SystemExit(
            "Authentication needs TLS and there is no keystore yet."
            + NEWLINE + "  python -m marketplace.tls issue")
    return [
        "",
        "# Authentication. Trino refuses to authenticate over plain HTTP, so the",
        "# TLS listener below is the only door that accepts a credential; the HTTP",
        "# port is left for the node talking to itself.",
        "http-server.https.enabled=true",
        "http-server.https.port=8443",
        "http-server.https.keystore.path=/etc/trino/tls/trino.p12",
        f"http-server.https.keystore.key={keystore_password()}",
        "http-server.authentication.type=OAUTH2,JWT",
        "",
        "# The browser flow, for people.",
        "#",
        "# Discovery is OFF deliberately. Left on, Trino fetches",
        "# <issuer>/.well-known/openid-configuration - and the issuer is the address",
        "# a BROWSER uses (localhost:8180), which inside this container resolves to",
        "# the container itself. Trino then fails to start with the memorable but",
        "# uninformative 'OAuth2 client not initialized'. The issuer is for",
        "# validating the token's `iss` claim; the URLs below are for actually",
        "# reaching Keycloak, and they are deliberately different addresses.",
        "http-server.authentication.oauth2.oidc.discovery=false",
        f"http-server.authentication.oauth2.issuer={settings['issuer']}",
        f"http-server.authentication.oauth2.auth-url={settings['auth_url']}",
        f"http-server.authentication.oauth2.token-url={settings['internal_token_url']}",
        f"http-server.authentication.oauth2.jwks-url={settings['internal_jwks_url']}",
        f"http-server.authentication.oauth2.client-id={settings['client_id']}",
        f"http-server.authentication.oauth2.client-secret={settings['client_secret']}",
        "http-server.authentication.oauth2.principal-field=preferred_username",
        "http-server.authentication.oauth2.scopes=openid",
        "",
        "# The bearer-token flow, for programs. Same realm, same principal claim.",
        f"http-server.authentication.jwt.key-file={settings['internal_jwks_url']}",
        f"http-server.authentication.jwt.required-issuer={settings['issuer']}",
        f"http-server.authentication.jwt.required-audience={settings['client_id']}",
        "http-server.authentication.jwt.principal-field=preferred_username",
        "",
        "# A session may only run as the principal in its own token, unless an",
        "# impersonation rule says otherwise. Trino calls checkCanImpersonateUser",
        "# whenever the requested user differs from the authenticated principal;",
        "# rules.json denies that for everyone by default, and never permits",
        "# becoming a platform principal at all.",
        "internal-communication.shared-secret=" + internal_secret(),
    ]


def impersonators(members):
    """Who may open a session as another user. OFF unless explicitly enabled.

    Impersonation is how support answers "what does this user actually see?"
    without sharing a password - but it is still the ability to act as someone
    else, so it is a deliberate switch rather than a default. Set
    MARKETPLACE_ALLOW_IMPERSONATION=1 to grant it to the administrator role.
    """
    if os.environ.get("MARKETPLACE_ALLOW_IMPERSONATION", "").lower() not in ("1", "true", "yes"):
        return ()
    # People, not engine identities. nda_dashboard is the API's principal and
    # marketplace_owner owns the views; neither is a support engineer, and a
    # compromised service account that can also become any user is a worse
    # outcome than one that cannot.
    from .build import SERVICE_PRINCIPALS
    return tuple(u for u in members.get("administrator", ())
                 if u not in SERVICE_PRINCIPALS)


def config_files(offline=False):
    """Every file the coordinator needs, as {relative path: contents}.

    Membership is resolved from Keycloak, not stated here. There is deliberately
    no second copy of "who is an analyst" in this repository to drift out of step
    with the directory.
    """
    # No silent default. Falling back to "minioadmin" produces a coordinator that
    # starts cleanly and then fails on every query with an unhelpful "failed to
    # get status for file" - the error points at storage when the fault is here.
    try:
        key = os.environ["AWS_ACCESS_KEY_ID"]
        secret = os.environ["AWS_SECRET_ACCESS_KEY"]
    except KeyError as missing:
        raise SystemExit(
            str(missing) + " is not set. Load the environment first:"
            + chr(10) + "  set -a && . ./streaming/.env && set +a") from None
    members = role_members(offline=offline)
    return {
        "config.properties": NEWLINE.join([
            "coordinator=true",
            "node-scheduler.include-coordinator=true",
            "http-server.http.port=8080",
            "discovery.uri=http://localhost:8080",
            "query.max-memory=2GB",
            "query.max-memory-per-node=1GB",
        ] + authentication_properties()) + NEWLINE,
        "node.properties": NEWLINE.join([
            "node.environment=marketplace",
            "node.id=marketplace-coordinator",
            "node.data-dir=/data/trino",
        ]) + NEWLINE,
        "jvm.config": NEWLINE.join([
            "-server", "-Xmx2G", "-XX:+UseG1GC", "-XX:G1HeapRegionSize=32M",
            # Keep this minimal: UseBiasedLocking was removed in modern JDKs and
            # an unrecognised flag stops the JVM before Trino logs anything useful.
            "-XX:+ExitOnOutOfMemoryError", "-Djdk.attach.allowAttachSelf=true",
        ]) + NEWLINE,
        "access-control.properties": NEWLINE.join([
            "# Rules generated from marketplace/products.py. Deny is the default,",
            "# so a schema with no matching rule is invisible, not merely refused.",
            "access-control.name=file",
            "security.config-file=/etc/trino/rules.json",
            "security.refresh-period=30s",
        ]) + NEWLINE,
        "group-provider.properties": NEWLINE.join([
            "# Maps users to marketplace roles. The file is rendered from Keycloak",
            "# group membership by marketplace/identity.py and re-read every 30s, so",
            "# a joiner, leaver or group change in the console takes effect without a",
            "# restart and without anyone editing an access rule.",
            "group-provider.name=file",
            "file.group-file=/etc/trino/groups.txt",
            "file.refresh-period=30s",
        ]) + NEWLINE,
        "groups.txt": group_file(members),
        "rules.json": json.dumps(access_rules(
            users=governed_users(members), impersonators=impersonators(members)),
            indent=2) + NEWLINE,
        "event-listener.properties": NEWLINE.join([
            "# Every completed query is POSTed to the audit collector, which keeps a",
            "# durable JSONL copy and batches them into an Iceberg table. Queries are",
            "# governed by the rules above; this is how they are also recorded.",
            "event-listener.name=http",
            "http-event-listener.log-completed=true",
            "http-event-listener.log-created=false",
            f"http-event-listener.connect-ingest-uri={AUDIT_URI}",
            "http-event-listener.connect-retry-count=3",
            "http-event-listener.connect-retry-delay=2s",
        ]) + NEWLINE,
        "catalog/iceberg.properties": NEWLINE.join([
            "connector.name=iceberg",
            "iceberg.catalog.type=rest",
            "iceberg.rest-catalog.uri=http://iceberg-rest:8181",
            "iceberg.rest-catalog.warehouse=s3://warehouse/",
            "fs.native-s3.enabled=true",
            "s3.endpoint=http://minio:9000",
            "s3.region=us-east-1",
            "s3.path-style-access=true",
            f"s3.aws-access-key={key}",
            f"s3.aws-secret-key={secret}",
        ]) + NEWLINE,
    }


def write(offline=False):
    root = OUT / "trino"
    rendered = config_files(offline=offline)
    for relative, content in rendered.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Docker's Linux health-check reads the port with grep/cut. CRLF leaves
        # a carriage return in its URL, even though Java accepts the config.
        target.write_text(content, encoding="utf-8", newline="\n")
    print(f"Wrote {len(rendered)} config files to {root}")
    return root


def authenticated():
    """Whether this deployment has an OIDC client, and so requires a token."""
    from .auth import NotAuthenticated, oidc
    try:
        oidc()
        return True
    except NotAuthenticated:
        return False


def loopback(host_port, container_port):
    """Publish a port on IPv4 and IPv6 loopback, never on a routable address."""
    return ["-p", f"127.0.0.1:{host_port}:{container_port}",
            "-p", f"[::1]:{host_port}:{container_port}"]


def up(offline=False):
    root = write(offline=offline).resolve()
    secure = authenticated()
    # BOTH loopback families. `localhost` resolves to ::1 before 127.0.0.1 on
    # Windows and on modern Linux, and the JVM honours that order, so binding
    # only 127.0.0.1 gives Java clients "Connection refused: getsockopt" against
    # an address the server never claimed. Python and the CLI happen to fall back
    # to IPv4; DBeaver does not.
    ports = loopback(HTTPS_PORT, 8443) if secure else []
    # The plain HTTP port is published ONLY while there is no authentication to
    # bypass. Once tokens are required, leaving it open would be an unauthenticated
    # door beside the locked one - which is the bug this whole change exists to fix.
    if not secure:
        ports += loopback(PORT, 8080)
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    result = subprocess.run([
        "docker", "run", "-d", "--name", CONTAINER,
        "--network", NETWORK, *ports,
        "-v", f"{root}:/etc/trino:ro",
        "--memory", "3g",
        IMAGE,
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("Could not start the coordinator: " + result.stderr[:400])
    where = f"https://localhost:{HTTPS_PORT}" if secure else f"http://127.0.0.1:{PORT}"
    print(f"Started {CONTAINER} on {where}"
          f" ({'authenticated' if secure else 'UNAUTHENTICATED'}); waiting for queries")
    for attempt in range(60):
        ok, output = query("admin", "SELECT 1")
        if ok:
            print(f"  ready after ~{attempt * 5}s")
            return
        time.sleep(5)
    raise SystemExit(f"Coordinator did not become ready. Last error: {output[:300]}"
                     + NEWLINE + f"  docker logs {CONTAINER} --tail 50")


def connection(user):
    """A client connection as `user`, authenticated if the cluster requires it."""
    import trino
    if authenticated():
        from .auth import connect
        return connect(user, catalog="iceberg")
    return trino.dbapi.connect(host="127.0.0.1", port=PORT, user=user,
                               catalog="iceberg", request_timeout=60)


def query(user, sql):
    """Run a statement as a named user. Returns (ok, output)."""
    try:
        conn = connection(user)
    except Exception as error:                      # noqa: BLE001 - reported, not raised
        return False, str(error).splitlines()[0]
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        return True, NEWLINE.join(" ".join(str(c) for c in row) for row in rows)
    except Exception as error:                      # noqa: BLE001
        return False, str(error).splitlines()[0]
    finally:
        conn.close()


def check():
    """Prove the two claims the marketplace rests on, per role."""
    cases = [
        # (label, user, sql, expect_success)
        ("business user sees the marketplace", "public.viewer",
         f"SHOW TABLES FROM iceberg.{MARKETPLACE_SCHEMA}", True),
        ("business user reads a certified view", "public.viewer",
         f"SELECT COUNT(*) FROM iceberg.{MARKETPLACE_SCHEMA}.application_throughput", True),
        ("business user CANNOT list physical schema", "public.viewer",
         "SHOW TABLES FROM iceberg.nda_gold", False),
        ("business user CANNOT read a physical table", "public.viewer",
         "SELECT COUNT(*) FROM iceberg.nda_gold.fact_ma_applications", False),
        ("business user CANNOT read restricted product", "public.viewer",
         f"SELECT COUNT(*) FROM iceberg.{MARKETPLACE_SCHEMA}.quality_metrics", False),
        ("analyst CAN read the restricted product", "alice.nakato",
         f"SELECT COUNT(*) FROM iceberg.{MARKETPLACE_SCHEMA}.quality_metrics", True),
        ("analyst CAN read curated physical layer", "alice.nakato",
         "SELECT COUNT(*) FROM iceberg.nda_gold.fact_ma_applications", True),
        ("analyst CANNOT read raw change history", "alice.nakato",
         "SELECT COUNT(*) FROM iceberg.nda_bronze.ma_applications", False),
        ("analyst CANNOT write to production", "alice.nakato",
         "DROP TABLE IF EXISTS iceberg.nda_gold.fact_ma_applications", False),
        ("engineer CAN read raw change history", "dana.okello",
         "SELECT COUNT(*) FROM iceberg.nda_bronze.ma_applications", True),
    ]
    failures = 0
    for label, user, sql, expect in cases:
        ok, output = query(user, sql)
        passed = ok == expect
        failures += not passed
        detail = "" if passed else f"  <- got {'success' if ok else output.splitlines()[0][:70]}"
        print(f"  [{'PASS' if passed else 'FAIL'}] {label:46} ({user}){detail}")
    print()
    print("ENFORCED" if not failures else f"{failures} rule(s) not behaving as designed")
    return failures == 0


def visible():
    """What each role actually sees when they look around."""
    for user in ("public.viewer", "alice.nakato", "sam.scientist", "dana.okello"):
        ok, output = query(user, "SHOW SCHEMAS FROM iceberg")
        schemas = [line.strip('"') for line in output.splitlines()
                   if line.strip('"') not in ("information_schema",)] if ok else ["<denied>"]
        print(f"  {user:16} sees: {', '.join(schemas)}")


def groups():
    """What Trino currently believes about every user it has been told about."""
    ok, output = query("admin", "SELECT 1")
    if not ok:
        raise SystemExit("Coordinator is not answering: " + output[:200])
    for role, users in sorted(role_members().items()):
        print(f"  {role:16} {len(users):>2}  {', '.join(users)}")


def main():
    parser = argparse.ArgumentParser(description="Dedicated marketplace Trino")
    parser.add_argument("action", choices=["write", "up", "check", "visible", "groups"])
    parser.add_argument("--offline", action="store_true",
                        help="Render from the seed cohort instead of Keycloak. Bootstrap only.")
    args = parser.parse_args()
    try:
        if args.action == "write":
            write(args.offline)
        elif args.action == "up":
            up(args.offline)
        elif args.action == "visible":
            visible()
        elif args.action == "groups":
            groups()
        else:
            raise SystemExit(0 if check() else 1)
    except DirectoryUnavailable as error:
        raise SystemExit(
            f"{error}{chr(10)}{chr(10)}"
            f"Nothing was written - the coordinator keeps the membership it already has."
            f"{chr(10)}Use --offline only to bootstrap before Keycloak exists.")


if __name__ == "__main__":
    main()
