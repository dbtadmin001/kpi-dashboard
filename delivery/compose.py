"""One service topology for isolated CI and the Linux VM; no external networks."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def topology(name, runtime, images, *, production=False, keycloak_url=None):
    if not re.fullmatch(r"nda-(ci-[a-z0-9-]+|staging|production)", name):
        raise ValueError("Use an isolated nda-ci-* project or nda-staging/nda-production")
    # An absolute POSIX path is left EXACTLY as given: it names a directory on
    # the host that will run this stack, which is not necessarily the host doing
    # the rendering. Resolving it here rewrote /var/lib/nda into a local path
    # when CI rendered a deployment for a VM. Only relative paths are resolved,
    # and only because they can only mean "here".
    runtime = (Path(runtime).as_posix() if Path(runtime).is_absolute()
               or str(runtime).startswith("/")
               else Path(runtime).resolve().as_posix())
    # `flink` and `catalog_base` in the lock are BUILD bases, not runtime images:
    # the build layers our Iceberg jars and the Postgres driver onto them. So a
    # caller must supply app/flink/catalog, and asking for one that was not
    # supplied is an error rather than a quiet fall back to a base image that
    # starts cleanly and processes nothing.
    supplied = json.loads((ROOT / "delivery/images.lock.json").read_text()) | images
    missing = [k for k in ("app", "flink", "catalog") if k not in images]
    if missing:
        raise ValueError(
            f"no built image supplied for {', '.join(missing)} - "
            "run `python -m delivery.release build` first")
    base = supplied
    if production and any("@sha256:" not in base[k] for k in ("app", "flink", "catalog")):
        raise ValueError("Deployment requires immutable app/flink/catalog image digests")
    shared = {
        "MARKETPLACE_RUNTIME_DIR": "/run/nda/marketplace",
        "MARKETPLACE_OIDC_FILE": "/run/nda/trino_oidc.json",
        "MARKETPLACE_CREDENTIALS_FILE": "/run/nda/credentials.json",
        "MARKETPLACE_CA_FILE": "/run/nda/marketplace/trino/tls/marketplace-ca.crt",
        "MARKETPLACE_REQUIRE_AUTH": "1",
        "MARKETPLACE_TRINO_HOST": "trino-marketplace",
        "KEYCLOAK_URL": "http://nda-keycloak:8180",
        "KEYCLOAK_ADMIN": "admin",
        "SQLSERVER_HOST": "nda-sqlserver", "SQLSERVER_PORT": "1433",
        "SQLSERVER_USER": "nda_simulator", "CONNECT_URL": "http://connect:8083",
        "AWS_REGION": "us-east-1", "KEYCLOAK_REALM": "nda",
        "OPA_URL": "http://opa:8181",
        # The audit receiver is a service on this network, not a host process.
        # Trino's event listener is rendered from this, so audit is now part of
        # the stack under test rather than something switched off for CI.
        "AUDIT_INGEST_URI": "http://audit:8099/v1/query-events",
        # The durable JSONL is the audit record of truth and the Iceberg table is
        # a projection of it, so it belongs on the mounted volume. The default is
        # inside the image, where a container restart would discard it.
        "AUDIT_LOG_PATH": "/run/nda/audit/query-events.jsonl",
    }
    secret_env = [f"{runtime}/secrets.env"]

    # Verified against each image before being written here.
    health = {
        "postgres": "pg_isready -U nda -d nda",
        "minio": "curl -fsS http://localhost:9000/minio/health/live",
        "keycloak": "exec 3<>/dev/tcp/localhost/8180",
        "connect": "curl -fsS http://localhost:8083/connectors",
        # Declare the image's own readiness probe in the generated Compose
        # document too, so services that consume Trino wait for it rather than
        # merely for its process to be spawned.
        "trino": "/usr/lib/trino/bin/health-check",
        # OPA is deliberately absent: its image is distroless and has no shell at
        # all, so no CMD-SHELL healthcheck can run in it. Its dependents fall back
        # to `service_started`, and readiness is proven by the first policy query.
        # This image has no curl and its /bin/sh is dash, so the obvious check
        # can never pass - and a healthcheck that can never pass is worse than
        # none, because depends_on then waits forever and the failure reads as
        # "iceberg-rest is unhealthy" rather than "your check is wrong".
        # bash is present, so this asks the kernel to open the port instead.
        # Readiness beyond "listening" is proven at the application level by
        # delivery/wait.py, which is the more honest place for it anyway.
        "iceberg-rest": "bash -c 'exec 3<>/dev/tcp/localhost/8181'",
    }

    def service(image, name=None, **kwargs):
        spec = {"image": base[image], "platform": "linux/amd64",
                "restart": "unless-stopped", **kwargs}
        if name in health:
            spec["healthcheck"] = {"test": ["CMD-SHELL", health[name]],
                                   "interval": "10s", "timeout": "5s",
                                   "retries": 30, "start_period": "30s"}
        return spec

    def app(**kwargs):
        return service("app", env_file=secret_env, environment=shared,
                       volumes=[f"{runtime}:/run/nda"], **kwargs)

    flink_properties = "\n".join([
        "jobmanager.rpc.address: jobmanager", "jobmanager.memory.process.size: 1024m",
        "taskmanager.memory.process.size: 5120m", "taskmanager.numberOfTaskSlots: 2",
        "taskmanager.memory.managed.fraction: 0.1", "parallelism.default: 1",
        "state.backend.type: rocksdb", "state.checkpoints.dir: file:///opt/flink/state/checkpoints",
        "state.savepoints.dir: file:///opt/flink/state/savepoints",
        "execution.checkpointing.interval: 60s",
        "execution.checkpointing.externalized-checkpoint-retention: RETAIN_ON_CANCELLATION",
    ])
    s = {
        "postgres": service("postgres", name="postgres", env_file=secret_env,
            environment={"POSTGRES_USER": "nda", "POSTGRES_DB": "nda"},
            volumes=["postgres:/var/lib/postgresql/data"], mem_limit="768m"),
        "minio": service("minio", name="minio", command=["server", "/data"], env_file=secret_env,
            volumes=["minio:/data"], mem_limit="1g"),
        "iceberg-rest": service("catalog", name="iceberg-rest", env_file=secret_env, environment={
            "CATALOG_CATALOG__IMPL": "org.apache.iceberg.jdbc.JdbcCatalog",
            "CATALOG_URI": "jdbc:postgresql://postgres:5432/nda",
            "CATALOG_JDBC_USER": "nda", "CATALOG_WAREHOUSE": "s3://warehouse/",
            "CATALOG_IO__IMPL": "org.apache.iceberg.aws.s3.S3FileIO",
            "CATALOG_S3_ENDPOINT": "http://minio:9000", "CATALOG_S3_PATH__STYLE__ACCESS": "true",
            "AWS_REGION": "us-east-1"}, mem_limit="1g"),
        "kafka": service("kafka", command=["redpanda", "start", "--smp=1", "--memory=768M",
            "--reserve-memory=0M", "--overprovisioned", "--node-id=0", "--check=false",
            "--kafka-addr=0.0.0.0:9092", "--advertise-kafka-addr=nda-kafka:9092"],
            networks={"default": {"aliases": ["nda-kafka"]}}, volumes=["kafka:/var/lib/redpanda/data"], mem_limit="1g"),
        "sqlserver": service("sqlserver", env_file=secret_env, environment={
            "ACCEPT_EULA": "Y", "MSSQL_PID": "${MSSQL_PID:-Developer}",
            "MSSQL_AGENT_ENABLED": "true", "MSSQL_MEMORY_LIMIT_MB": "2048"},
            networks={"default": {"aliases": ["nda-sqlserver"]}}, volumes=["sqlserver:/var/opt/mssql"], mem_limit="3g"),
        "connect": service("connect", name="connect", environment={
            "BOOTSTRAP_SERVERS": "nda-kafka:9092", "GROUP_ID": "nda-connect-v1",
            "CONFIG_STORAGE_TOPIC": "nda.connect.configs", "OFFSET_STORAGE_TOPIC": "nda.connect.offsets",
            "STATUS_STORAGE_TOPIC": "nda.connect.status", "CONFIG_STORAGE_REPLICATION_FACTOR": "1",
            "OFFSET_STORAGE_REPLICATION_FACTOR": "1", "STATUS_STORAGE_REPLICATION_FACTOR": "1",
            "HEAP_OPTS": "-Xms256m -Xmx512m"}, mem_limit="1g"),
        # The Flink worker runs as UID 999. Docker creates a named volume as
        # root, so initialize its ownership before any stateful JVM starts.
        "flink-init": service("flink", entrypoint=["bash", "-lc"], command=[
            "mkdir -p /opt/flink/state/checkpoints /opt/flink/state/savepoints && "
            "chown -R 999:999 /opt/flink/state"], user="0:0",
            volumes=["flink-state:/opt/flink/state"], mem_limit="256m", restart="no"),
        "jobmanager": service("flink", command="jobmanager", env_file=secret_env,
            environment={"FLINK_PROPERTIES": flink_properties}, volumes=["flink-state:/opt/flink/state"], mem_limit="1400m"),
        "taskmanager": service("flink", command="taskmanager", env_file=secret_env,
            environment={"FLINK_PROPERTIES": flink_properties}, volumes=["flink-state:/opt/flink/state"], mem_limit="5632m"),
        # Submit the SQL StatementSet after the catalog and source have been
        # provisioned.  A JobManager and TaskManager alone do no processing;
        # without this service a green Compose boot still serves empty tables.
        "flink-sql": service("flink", command=["bash", "-lc",
            "mkdir -p /opt/flink/state/checkpoints /opt/flink/state/savepoints && "
            "exec /opt/flink/bin/sql-client.sh -f /opt/nda/medallion.sql"],
            env_file=secret_env, environment={"FLINK_PROPERTIES": flink_properties},
            volumes=["flink-state:/opt/flink/state"], mem_limit="1400m", restart="no"),
        "keycloak": service("keycloak", name="keycloak", env_file=secret_env,
            command=["start", "--http-enabled=true", "--http-port=8180",
                     "--hostname=" + (keycloak_url or "http://nda-keycloak:8180"),
                     "--hostname-backchannel-dynamic=true"],
            environment={"KC_DB": "postgres", "KC_DB_URL": "jdbc:postgresql://postgres:5432/nda",
                         "KC_DB_USERNAME": "nda", "KC_BOOTSTRAP_ADMIN_USERNAME": "admin",
                         "JAVA_OPTS_KC_HEAP": "-Xms256m -Xmx768m"},
            networks={"default": {"aliases": ["nda-keycloak"]}}, mem_limit="1200m"),
        "trino": service("trino", volumes=[f"{runtime}/marketplace/trino:/etc/trino:ro"],
            networks={"default": {"aliases": ["trino-marketplace"]}}, mem_limit="3g"),
        # The audit receiver. Without it in the topology, CI had to delete the
        # event listener - which meant the one control that records who read what
        # was the only thing never exercised before release.
        "audit": app(command=["python", "-m", "marketplace.audit", "serve"]),
        # streaming/access.py asks OPA for every authorization decision, and the
        # topology had no OPA in it - so OPA_URL pointed at nothing and every
        # entitlement check in a deployed stack would have failed. The policy and
        # its data are Terraform output, mounted from the runtime directory.
        "opa": service("opa", name="opa",
            command=["run", "--server", "--addr=0.0.0.0:8181", "--log-level=error",
                     "/policy/authz.rego", "/policy/data.json"],
            volumes=[f"{runtime}/policy:/policy:ro"], mem_limit="256m"),
        "api": app(command=["python", "-m", "uvicorn", "streaming.api:app", "--host", "0.0.0.0", "--port", "8000"]),
        "identity": app(command=["python", "-m", "marketplace.identity", "watch"]),
        "tools": app(command=["python", "-m", "delivery.integration"], profiles=["tools"]),
    }
    # One PostgreSQL instance hosts both schemas for this single-VM baseline.
    # Catalog tables and Keycloak tables are distinct; backups cover both.
    if production:
        if not keycloak_url or not keycloak_url.startswith("https://"):
            raise ValueError("VM deployments require a public HTTPS Keycloak URL")
        s["keycloak"]["command"] += ["--https-port=8444", "--https-certificate-file=/tls/keycloak.crt",
                                       "--https-certificate-key-file=/tls/keycloak.key"]
        s["keycloak"]["volumes"] = [f"{runtime}/public-tls:/tls:ro"]
        s["keycloak"]["ports"] = ["8444:8444"]
        s["trino"]["ports"] = ["8443:8443"]
        s["api"]["ports"] = ["127.0.0.1:8000:8000"]
    # Ordering only. Application-level polling (delivery/wait.py) is still what
    # proves readiness - this just stops a cold VM boot from thrashing through
    # restart loops while dependencies come up.
    ordering = {
        "iceberg-rest": ["postgres", "minio"],
        "keycloak": ["postgres"],
        "connect": ["kafka", "sqlserver"],
        "jobmanager": ["kafka", "iceberg-rest", "flink-init"],
        "taskmanager": ["jobmanager"],
        "flink-sql": ["jobmanager", "taskmanager", "iceberg-rest", "kafka"],
        "trino": ["iceberg-rest", "keycloak"],
        "audit": ["trino"],
        "api": ["trino", "opa"],
        "identity": ["keycloak", "trino"],
        "tools": ["trino", "keycloak", "connect", "jobmanager", "flink-sql", "audit"],
    }
    for name, upstreams in ordering.items():
        s[name]["depends_on"] = {
            up: {"condition": ("service_completed_successfully" if up == "flink-init"
                                else "service_healthy" if "healthcheck" in s[up]
                                else "service_started")}
            for up in upstreams}
    for v in s.values():
        v["logging"] = {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}
    return {"name": name, "services": s,
            "volumes": {v: {} for v in ["postgres", "minio", "kafka", "sqlserver", "flink-state"]},
            "networks": {"default": {}}}
