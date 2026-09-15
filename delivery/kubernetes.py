"""Kubernetes manifests, generated from the same declarations as the Compose stack.

The previous manifests were hand-written and drifted: they shipped a Trino with
no access control, no group provider and no OIDC, no API, no audit collector and
no identity sync, referencing tag-only images nothing in the pipeline built.
Deploying them would have undone the governance rather than distributed it.

So these are generated - from `images.lock.json` and `images.built.json` like the
Compose topology, and from `marketplace/` for the governed Trino configuration.
The two deployment targets cannot disagree about what the platform is, because
neither is written down twice.

    python -m delivery.kubernetes render --nodes 3 -o infra/k8s/generated.yaml
    python -m delivery.kubernetes render --nodes 3 | kubectl apply -f -

WHAT REPLICATES, AND WHY

Not everything benefits from another copy, and for several of these a second
copy is actively wrong.

  Scales with the cluster - more nodes means more capacity:
    minio            erasure-coded object store; needs 4 to survive losing one
    kafka            3 brokers, replication factor 3
    trino-worker     query execution; the coordinator stays single
    flink-taskmanager  stream processing slots

  Replicated for availability - stateless, any replica will do:
    api, opa, iceberg-rest, keycloak

  Deliberately single:
    trino-coordinator  holds the access rules; two would need identical state
                       and gain nothing, since workers do the work
    flink-jobmanager   single job manager per job without ZooKeeper HA
    connect            one Debezium task per source; a second would duplicate
                       every change event
    audit              writes the durable JSONL that IS the audit record; a
                       second writer would interleave into the same file
    postgres, sqlserver  single writers, backed by a volume
"""
import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLATFORM = "nda-platform"
PIPELINE = "nda-pipeline"

# Generated config lands here and Trino reads it from the `trino` subdirectory,
# which is what MARKETPLACE_RUNTIME_DIR produces.
GENERATED = "/etc/marketplace"


def images(built=None):
    base = json.loads((ROOT / "delivery/images.lock.json").read_text(encoding="utf-8"))
    path = ROOT / "delivery/images.built.json"
    if built is None and path.exists():
        built = json.loads(path.read_text(encoding="utf-8"))
    return base | (built or {})


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------
def _probe(kind, port=None, path=None, command=None, scheme="HTTP",
           delay=10, period=10, failures=6, timeout=5):
    """A probe that answers a real question, not just 'is the port open'."""
    probe = {"initialDelaySeconds": delay, "periodSeconds": period,
             "failureThreshold": failures, "timeoutSeconds": timeout}
    if command:
        probe["exec"] = {"command": ["sh", "-c", command]}
    else:
        probe["httpGet"] = {"path": path, "port": port, "scheme": scheme}
    return probe


def _resources(cpu, memory, cpu_limit=None, memory_limit=None):
    return {"requests": {"cpu": cpu, "memory": memory},
            "limits": {"cpu": cpu_limit or cpu, "memory": memory_limit or memory}}


def _spread(app, whenUnsatisfiable="ScheduleAnyway"):
    """Spread replicas over nodes.

    ScheduleAnyway rather than DoNotSchedule: on a three-node cluster a hard
    constraint leaves pods Pending forever the moment replicas exceed nodes,
    which looks like a broken deployment and is really a broken constraint.
    """
    return [{"maxSkew": 1, "topologyKey": "kubernetes.io/hostname",
             "whenUnsatisfiable": whenUnsatisfiable,
             "labelSelector": {"matchLabels": {"app": app}}}]


def _secret_env(*names, secret="nda-secrets"):
    return [{"name": n, "valueFrom": {"secretKeyRef": {"name": secret, "key": n}}}
            for n in names]


def _workload(kind, name, namespace, app_image, replicas, *, containers=None,
              volumes=None, claims=None, service_name=None, spread=True,
              init=None, node_selector=None):
    pod = {"metadata": {"labels": {"app": name}},
           "spec": {"containers": containers or [],
                    "terminationGracePeriodSeconds": 60}}
    if init:
        pod["spec"]["initContainers"] = init
    if volumes:
        pod["spec"]["volumes"] = volumes
    if spread and replicas > 1:
        pod["spec"]["topologySpreadConstraints"] = _spread(name)
    if node_selector:
        pod["spec"]["nodeSelector"] = node_selector
    spec = {"replicas": replicas,
            "selector": {"matchLabels": {"app": name}},
            "template": pod}
    if kind == "StatefulSet":
        spec["serviceName"] = service_name or name
        spec["podManagementPolicy"] = "Parallel"
        if claims:
            spec["volumeClaimTemplates"] = claims
    return {"apiVersion": "apps/v1", "kind": kind,
            "metadata": {"name": name, "namespace": namespace,
                         "labels": {"app": name}},
            "spec": spec}


def _service(name, namespace, ports, headless=False, selector=None):
    spec = {"selector": {"app": selector or name},
            "ports": [{"name": n, "port": p, "targetPort": p} for n, p in ports]}
    if headless:
        spec["clusterIP"] = "None"
    return {"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": name, "namespace": namespace}, "spec": spec}


def _pdb(name, namespace, min_available=None, max_unavailable=1):
    """Keep a voluntary disruption (a drain, an upgrade) from taking quorum."""
    spec = {"selector": {"matchLabels": {"app": name}}}
    if min_available is not None:
        spec["minAvailable"] = min_available
    else:
        spec["maxUnavailable"] = max_unavailable
    return {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
            "metadata": {"name": name, "namespace": namespace}, "spec": spec}


def _claim(name, size, storage_class="nda-local"):
    return {"metadata": {"name": name},
            "spec": {"accessModes": ["ReadWriteOnce"],
                     "storageClassName": storage_class,
                     "resources": {"requests": {"storage": size}}}}


# --------------------------------------------------------------------------
# The stack
# --------------------------------------------------------------------------
def base():
    yield {"apiVersion": "v1", "kind": "Namespace",
           "metadata": {"name": PLATFORM, "labels": {"nda.io/layer": "platform"}}}
    yield {"apiVersion": "v1", "kind": "Namespace",
           "metadata": {"name": PIPELINE, "labels": {"nda.io/layer": "pipeline"}}}
    # Node-local storage: MinIO replicates across nodes itself, so a shared
    # filesystem underneath would duplicate the replication and halve capacity.
    yield {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
           "metadata": {"name": "nda-local"},
           "provisioner": "rancher.io/local-path",
           "volumeBindingMode": "WaitForFirstConsumer",
           "reclaimPolicy": "Retain"}


def data_plane(image, nodes, storage_nodes):
    """Stateful singletons and the two things that genuinely scale out."""
    # ---- Postgres: the Iceberg catalog and Keycloak share it.
    yield _workload("StatefulSet", "postgres", PLATFORM, image, 1,
        containers=[{
            "name": "postgres", "image": image["postgres"],
            "ports": [{"containerPort": 5432}],
            "env": [{"name": "POSTGRES_USER", "value": "nda"},
                    {"name": "POSTGRES_DB", "value": "nda"},
                    {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
                    *_secret_env("POSTGRES_PASSWORD")],
            "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql/data"}],
            "resources": _resources("250m", "512Mi", "1", "1Gi"),
            "readinessProbe": _probe("exec", command="pg_isready -U nda -d nda"),
            "livenessProbe": _probe("exec", command="pg_isready -U nda -d nda",
                                    delay=60, failures=6)}],
        claims=[_claim("data", "20Gi")])
    yield _service("postgres", PLATFORM, [("postgres", 5432)])

    # ---- MinIO: this is the distributed storage.
    # Erasure coding needs four drives to survive losing one. Below that it runs
    # single-node, which is a valid object store and not a fault-tolerant one.
    minio_replicas = storage_nodes if storage_nodes >= 4 else 1
    distributed = minio_replicas >= 4
    command = ["server", "--console-address", ":9001"]
    if distributed:
        command.append(f"http://minio-{{0...{minio_replicas - 1}}}.minio-headless.{PLATFORM}.svc.cluster.local/data")
    else:
        command.append("/data")
    yield _workload("StatefulSet", "minio", PLATFORM, image, minio_replicas,
        service_name="minio-headless",
        node_selector={"nda.io/storage": "true"},
        containers=[{
            "name": "minio", "image": image["minio"], "command": command,
            "ports": [{"containerPort": 9000}, {"containerPort": 9001}],
            "env": _secret_env("MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"),
            "volumeMounts": [{"name": "data", "mountPath": "/data"}],
            "resources": _resources("250m", "512Mi", "2", "2Gi"),
            # /ready is the one that knows about quorum; /live only knows the
            # process is up, which during an erasure-set rebuild is not the same.
            "readinessProbe": _probe("http", 9000, "/minio/health/ready"),
            "livenessProbe": _probe("http", 9000, "/minio/health/live",
                                    delay=60, failures=6)}],
        claims=[_claim("data", "100Gi")])
    yield _service("minio", PLATFORM, [("api", 9000), ("console", 9001)])
    yield _service("minio-headless", PLATFORM, [("api", 9000)], headless=True, selector="minio")
    if distributed:
        yield _pdb("minio", PLATFORM, max_unavailable=1)

    # ---- Kafka (Redpanda): the streaming log.
    brokers = min(nodes, 3)
    yield _workload("StatefulSet", "kafka", PLATFORM, image, brokers,
        service_name="kafka-headless",
        containers=[{
            "name": "redpanda", "image": image["kafka"],
            "command": ["redpanda", "start", "--smp=1", "--memory=768M",
                        "--reserve-memory=0M", "--overprovisioned", "--check=false",
                        "--kafka-addr=0.0.0.0:9092",
                        f"--advertise-kafka-addr=$(POD_NAME).kafka-headless.{PLATFORM}.svc.cluster.local:9092",
                        f"--seeds=kafka-0.kafka-headless.{PLATFORM}.svc.cluster.local:33145"],
            "ports": [{"containerPort": 9092}, {"containerPort": 9644}, {"containerPort": 33145}],
            "env": [{"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}}],
            "volumeMounts": [{"name": "data", "mountPath": "/var/lib/redpanda/data"}],
            "resources": _resources("250m", "1Gi", "1", "1500Mi"),
            "readinessProbe": _probe("http", 9644, "/v1/status/ready", delay=20),
            "livenessProbe": _probe("http", 9644, "/v1/status/ready", delay=90, failures=6)}],
        claims=[_claim("data", "50Gi")])
    yield _service("kafka", PLATFORM, [("kafka", 9092), ("admin", 9644)])
    yield _service("kafka-headless", PLATFORM,
                   [("kafka", 9092), ("rpc", 33145)], headless=True, selector="kafka")
    if brokers >= 3:
        # Quorum, not just "one at a time": with 3 brokers, minAvailable 2 keeps
        # replication factor 3 writable through a drain.
        yield _pdb("kafka", PLATFORM, min_available=2)

    # ---- SQL Server: the source system CDC reads from.
    yield _workload("StatefulSet", "sqlserver", PIPELINE, image, 1,
        containers=[{
            "name": "sqlserver", "image": image["sqlserver"],
            "ports": [{"containerPort": 1433}],
            "env": [{"name": "ACCEPT_EULA", "value": "Y"},
                    {"name": "MSSQL_PID", "value": "Developer"},
                    {"name": "MSSQL_AGENT_ENABLED", "value": "true"},
                    *_secret_env("MSSQL_SA_PASSWORD")],
            "volumeMounts": [{"name": "data", "mountPath": "/var/opt/mssql"}],
            "resources": _resources("500m", "2Gi", "2", "3Gi"),
            "readinessProbe": _probe("exec", delay=45, period=15, failures=20,
                command="/opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa "
                        "-P \"$MSSQL_SA_PASSWORD\" -Q 'SELECT 1'")}],
        claims=[_claim("data", "50Gi")])
    yield _service("sqlserver", PIPELINE, [("mssql", 1433)])


def catalog_and_identity(image, nodes, keycloak_url):
    replicas = min(nodes, 2)

    # ---- Iceberg REST catalog: stateless over Postgres, so it replicates.
    yield _workload("Deployment", "iceberg-rest", PLATFORM, image, replicas,
        containers=[{
            "name": "catalog", "image": image["catalog"],
            "ports": [{"containerPort": 8181}],
            "env": [
                {"name": "CATALOG_CATALOG__IMPL", "value": "org.apache.iceberg.jdbc.JdbcCatalog"},
                {"name": "CATALOG_URI", "value": f"jdbc:postgresql://postgres.{PLATFORM}.svc:5432/nda"},
                {"name": "CATALOG_JDBC_USER", "value": "nda"},
                {"name": "CATALOG_WAREHOUSE", "value": "s3://warehouse/"},
                {"name": "CATALOG_IO__IMPL", "value": "org.apache.iceberg.aws.s3.S3FileIO"},
                {"name": "CATALOG_S3_ENDPOINT", "value": f"http://minio.{PLATFORM}.svc:9000"},
                {"name": "CATALOG_S3_PATH__STYLE__ACCESS", "value": "true"},
                {"name": "AWS_REGION", "value": "us-east-1"},
                *_secret_env("CATALOG_JDBC_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")],
            "resources": _resources("100m", "512Mi", "1", "1Gi"),
            "readinessProbe": _probe("http", 8181, "/v1/config", delay=20),
            "livenessProbe": _probe("http", 8181, "/v1/config", delay=60, failures=6)}])
    yield _service("iceberg-rest", PLATFORM, [("rest", 8181)])
    if replicas > 1:
        yield _pdb("iceberg-rest", PLATFORM, max_unavailable=1)

    # ---- Keycloak: the directory. Replicated, sharing the Postgres above.
    yield _workload("Deployment", "keycloak", PLATFORM, image, replicas,
        containers=[{
            "name": "keycloak", "image": image["keycloak"],
            "args": ["start", "--http-enabled=true", "--http-port=8180",
                     f"--hostname={keycloak_url}",
                     "--hostname-backchannel-dynamic=true", "--cache=ispn"],
            "ports": [{"containerPort": 8180}, {"containerPort": 9000}],
            "env": [{"name": "KC_DB", "value": "postgres"},
                    {"name": "KC_DB_URL", "value": f"jdbc:postgresql://postgres.{PLATFORM}.svc:5432/nda"},
                    {"name": "KC_DB_USERNAME", "value": "nda"},
                    {"name": "KC_HEALTH_ENABLED", "value": "true"},
                    {"name": "KC_BOOTSTRAP_ADMIN_USERNAME", "value": "admin"},
                    {"name": "JAVA_OPTS_KC_HEAP", "value": "-Xms256m -Xmx768m"},
                    *_secret_env("KC_DB_PASSWORD", "KEYCLOAK_ADMIN_PASSWORD")],
            "resources": _resources("250m", "768Mi", "1", "1200Mi"),
            # Keycloak's own readiness endpoint on the management port: it knows
            # whether the database migration finished, which a TCP check does not.
            "readinessProbe": _probe("http", 9000, "/health/ready", delay=30, failures=20),
            "livenessProbe": _probe("http", 9000, "/health/live", delay=120, failures=6)}])
    yield _service("keycloak", PLATFORM, [("http", 8180)])
    yield _service("nda-keycloak", PLATFORM, [("http", 8180)], selector="keycloak")
    if replicas > 1:
        yield _pdb("keycloak", PLATFORM, max_unavailable=1)

    # ---- OPA: dashboard entitlements, rendered by Terraform into a ConfigMap.
    yield _workload("Deployment", "opa", PIPELINE, image, replicas,
        containers=[{
            "name": "opa", "image": image["opa"],
            "args": ["run", "--server", "--addr=0.0.0.0:8181", "--log-level=error",
                     "/policy/authz.rego", "/policy/data.json"],
            "ports": [{"containerPort": 8181}],
            "volumeMounts": [{"name": "policy", "mountPath": "/policy", "readOnly": True}],
            "resources": _resources("50m", "128Mi", "500m", "256Mi"),
            "readinessProbe": _probe("http", 8181, "/health"),
            "livenessProbe": _probe("http", 8181, "/health", delay=30, failures=6)}],
        volumes=[{"name": "policy", "configMap": {"name": "opa-policy"}}])
    yield _service("opa", PIPELINE, [("http", 8181)])


def trino(image, nodes):
    """The governed door.

    The coordinator renders its own configuration at start-up and keeps its group
    file current from Keycloak, exactly as the Compose stack does - an init
    container for the one-time render, a sidecar for the continuous refresh, and
    an emptyDir they share with Trino. No ConfigMap, because the rules are
    derived from the directory at runtime and a ConfigMap would be a stale copy.
    """
    shared = [{"name": "config", "emptyDir": {}}]
    marketplace_env = [
        {"name": "MARKETPLACE_RUNTIME_DIR", "value": GENERATED},
        {"name": "MARKETPLACE_OIDC_FILE", "value": f"{GENERATED}/trino_oidc.json"},
        {"name": "MARKETPLACE_CREDENTIALS_FILE", "value": f"{GENERATED}/credentials.json"},
        {"name": "MARKETPLACE_CA_FILE", "value": f"{GENERATED}/trino/tls/marketplace-ca.crt"},
        {"name": "MARKETPLACE_TRINO_HOST", "value": f"trino.{PLATFORM}.svc"},
        {"name": "KEYCLOAK_URL", "value": f"http://nda-keycloak.{PLATFORM}.svc:8180"},
        {"name": "KEYCLOAK_ADMIN", "value": "admin"},
        {"name": "AUDIT_INGEST_URI", "value": f"http://audit.{PIPELINE}.svc:8099/v1/query-events"},
        *_secret_env("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "KEYCLOAK_ADMIN_PASSWORD"),
    ]
    mount = [{"name": "config", "mountPath": GENERATED}]

    yield _workload("Deployment", "trino-coordinator", PLATFORM, image, 1, spread=False,
        volumes=shared,
        init=[{
            # Renders config.properties, rules.json, groups.txt and the TLS
            # keystore into the shared volume before Trino starts.
            "name": "render-config", "image": image["app"],
            "command": ["sh", "-c",
                        "python -m marketplace.tls issue && "
                        "python -m marketplace.trino_config write"],
            "env": marketplace_env, "volumeMounts": mount,
            "resources": _resources("100m", "256Mi")}],
        containers=[
            {"name": "trino", "image": image["trino"],
             "ports": [{"containerPort": 8443}, {"containerPort": 8080}],
             "volumeMounts": [{"name": "config", "mountPath": "/etc/trino", "subPath": "trino"}],
             "resources": _resources("500m", "2Gi", "2", "3Gi"),
             # /v1/info reports `starting`, so this waits for a coordinator that
             # can actually plan a query rather than one that has merely bound.
             "readinessProbe": _probe("http", 8443, "/v1/info", scheme="HTTPS",
                                      delay=30, failures=30),
             "livenessProbe": _probe("http", 8443, "/v1/info", scheme="HTTPS",
                                     delay=180, failures=6)},
            {"name": "identity", "image": image["app"],
             "command": ["python", "-m", "marketplace.identity", "watch", "--interval", "30"],
             "env": marketplace_env, "volumeMounts": mount,
             "resources": _resources("50m", "256Mi", "200m", "512Mi")}])
    yield _service("trino", PLATFORM, [("https", 8443)], selector="trino-coordinator")
    yield _service("trino-marketplace", PLATFORM, [("https", 8443)], selector="trino-coordinator")

    # ---- Workers: this is the query compute that scales with the cluster.
    # Workers hold no access rules - authorisation happens on the coordinator -
    # so they need none of the marketplace configuration.
    workers = max(1, nodes - 1)
    yield {"apiVersion": "v1", "kind": "ConfigMap",
           "metadata": {"name": "trino-worker", "namespace": PLATFORM},
           "data": {"config.properties": "\n".join([
                        "coordinator=false", "http-server.http.port=8080",
                        f"discovery.uri=http://trino-discovery.{PLATFORM}.svc:8080",
                        "query.max-memory=4GB", "query.max-memory-per-node=1GB"]) + "\n",
                    "node.properties": "node.environment=marketplace\n",
                    "jvm.config": "\n".join([
                        "-server", "-Xmx2G", "-XX:+UseG1GC",
                        "-XX:G1HeapRegionSize=32M", "-XX:+ExitOnOutOfMemoryError"]) + "\n"}}
    yield _workload("Deployment", "trino-worker", PLATFORM, image, workers,
        volumes=[{"name": "config", "configMap": {"name": "trino-worker"}},
                 {"name": "catalog", "emptyDir": {}},
                 # Somewhere for the init container to render into before it
                 # copies out only the catalog files a worker needs.
                 {"name": "scratch", "emptyDir": {}}],
        init=[{"name": "render-catalog", "image": image["app"],
               "command": ["sh", "-c",
                           f"python -m marketplace.trino_config write && "
                           f"cp {GENERATED}/trino/catalog/*.properties /catalog/"],
               "env": marketplace_env,
               "volumeMounts": [{"name": "catalog", "mountPath": "/catalog"},
                                {"name": "scratch", "mountPath": GENERATED}],
               "resources": _resources("100m", "256Mi")}],
        containers=[{
            "name": "trino", "image": image["trino"],
            "ports": [{"containerPort": 8080}],
            "volumeMounts": [{"name": "config", "mountPath": "/etc/trino/config.properties", "subPath": "config.properties"},
                             {"name": "config", "mountPath": "/etc/trino/node.properties", "subPath": "node.properties"},
                             {"name": "config", "mountPath": "/etc/trino/jvm.config", "subPath": "jvm.config"},
                             {"name": "catalog", "mountPath": "/etc/trino/catalog"}],
            "resources": _resources("500m", "2Gi", "2", "3Gi"),
            "readinessProbe": _probe("http", 8080, "/v1/info", delay=30, failures=30),
            "livenessProbe": _probe("http", 8080, "/v1/info", delay=180, failures=6)}])
    # The coordinator's plain HTTP port is how workers find it; it carries no
    # client traffic and is not published outside the cluster.
    yield _service("trino-discovery", PLATFORM, [("http", 8080)], selector="trino-coordinator")
    if workers > 1:
        yield _pdb("trino-worker", PLATFORM, max_unavailable=1)


def pipeline(image, nodes):
    flink_properties = "\n".join([
        f"jobmanager.rpc.address: flink-jobmanager.{PIPELINE}.svc",
        "jobmanager.memory.process.size: 1024m",
        "taskmanager.memory.process.size: 5120m",
        "taskmanager.numberOfTaskSlots: 2",
        "taskmanager.memory.managed.fraction: 0.1",
        "parallelism.default: 1",
        "state.backend.type: rocksdb",
        "state.checkpoints.dir: file:///opt/flink/state/checkpoints",
        "state.savepoints.dir: file:///opt/flink/state/savepoints",
        "execution.checkpointing.interval: 60s",
        "execution.checkpointing.externalized-checkpoint-retention: RETAIN_ON_CANCELLATION"])
    flink_env = [{"name": "FLINK_PROPERTIES", "value": flink_properties},
                 {"name": "AWS_REGION", "value": "us-east-1"},
                 *_secret_env("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")]

    # One job manager per job. HA would need ZooKeeper or the Kubernetes HA
    # services; until then a second replica is a split brain, not availability.
    yield _workload("StatefulSet", "flink-jobmanager", PIPELINE, image, 1,
        containers=[{
            "name": "jobmanager", "image": image["flink"], "args": ["jobmanager"],
            "ports": [{"containerPort": 8081}, {"containerPort": 6123}],
            "env": flink_env,
            "volumeMounts": [{"name": "state", "mountPath": "/opt/flink/state"}],
            "resources": _resources("250m", "1Gi", "1", "1400Mi"),
            "readinessProbe": _probe("http", 8081, "/overview", delay=20),
            "livenessProbe": _probe("http", 8081, "/overview", delay=90, failures=6)}],
        claims=[_claim("state", "20Gi")])
    yield _service("flink-jobmanager", PIPELINE, [("ui", 8081), ("rpc", 6123)])

    # Task managers ARE the distributed processing: one per compute node.
    yield _workload("Deployment", "flink-taskmanager", PIPELINE, image, max(1, nodes - 1),
        containers=[{
            "name": "taskmanager", "image": image["flink"], "args": ["taskmanager"],
            "env": flink_env,
            "resources": _resources("500m", "5Gi", "2", "5632Mi"),
            "readinessProbe": _probe("exec", delay=30,
                                     command="ps aux | grep -q '[T]askManagerRunner'")}])

    # One Debezium task per source table set. A second replica would read the
    # same log positions and emit every change event twice.
    yield _workload("Deployment", "connect", PIPELINE, image, 1, spread=False,
        containers=[{
            "name": "connect", "image": image["connect"],
            "ports": [{"containerPort": 8083}],
            "env": [{"name": "BOOTSTRAP_SERVERS", "value": f"kafka.{PLATFORM}.svc:9092"},
                    {"name": "GROUP_ID", "value": "nda-connect-v1"},
                    {"name": "CONFIG_STORAGE_TOPIC", "value": "nda.connect.configs"},
                    {"name": "OFFSET_STORAGE_TOPIC", "value": "nda.connect.offsets"},
                    {"name": "STATUS_STORAGE_TOPIC", "value": "nda.connect.status"},
                    {"name": "CONFIG_STORAGE_REPLICATION_FACTOR", "value": str(min(nodes, 3))},
                    {"name": "OFFSET_STORAGE_REPLICATION_FACTOR", "value": str(min(nodes, 3))},
                    {"name": "STATUS_STORAGE_REPLICATION_FACTOR", "value": str(min(nodes, 3))},
                    {"name": "HEAP_OPTS", "value": "-Xms256m -Xmx512m"}],
            "resources": _resources("250m", "768Mi", "1", "1Gi"),
            "readinessProbe": _probe("http", 8083, "/connectors", delay=30, failures=20),
            "livenessProbe": _probe("http", 8083, "/", delay=120, failures=6)}])
    yield _service("connect", PIPELINE, [("rest", 8083)])


def application(image, nodes):
    common_env = [
        {"name": "MARKETPLACE_RUNTIME_DIR", "value": GENERATED},
        {"name": "MARKETPLACE_TRINO_HOST", "value": f"trino.{PLATFORM}.svc"},
        {"name": "MARKETPLACE_REQUIRE_AUTH", "value": "1"},
        {"name": "TRINO_HOST", "value": f"trino.{PLATFORM}.svc"},
        {"name": "KEYCLOAK_URL", "value": f"http://nda-keycloak.{PLATFORM}.svc:8180"},
        {"name": "OPA_URL", "value": f"http://opa.{PIPELINE}.svc:8181"},
        *_secret_env("NDA_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
    ]

    # The serving API is stateless, so it replicates for availability.
    yield _workload("Deployment", "api", PIPELINE, image, min(nodes, 2),
        containers=[{
            "name": "api", "image": image["app"],
            "command": ["python", "-m", "uvicorn", "streaming.api:app",
                        "--host", "0.0.0.0", "--port", "8000"],
            "ports": [{"containerPort": 8000}],
            "env": common_env,
            "resources": _resources("100m", "512Mi", "1", "1Gi"),
            # /health/ready asks Trino a real question, so a replica that cannot
            # serve is removed from the Service rather than failing requests.
            "readinessProbe": _probe("http", 8000, "/health/ready", delay=20, failures=30),
            "livenessProbe": _probe("http", 8000, "/health/live", delay=60, failures=6)}])
    yield _service("api", PIPELINE, [("http", 8000)])
    if min(nodes, 2) > 1:
        yield _pdb("api", PIPELINE, max_unavailable=1)

    # Single writer: the durable JSONL it appends IS the audit record, and the
    # Iceberg table is a projection of it. Two replicas would interleave.
    yield _workload("StatefulSet", "audit", PIPELINE, image, 1, spread=False,
        containers=[{
            "name": "audit", "image": image["app"],
            "command": ["python", "-m", "marketplace.audit", "serve"],
            "ports": [{"containerPort": 8099}],
            "env": [*common_env,
                    {"name": "AUDIT_LOG_PATH", "value": "/var/lib/audit/query-events.jsonl"}],
            "volumeMounts": [{"name": "audit", "mountPath": "/var/lib/audit"}],
            "resources": _resources("100m", "256Mi", "500m", "512Mi"),
            "readinessProbe": _probe("http", 8099, "/v1/query-events", delay=20, failures=30)}],
        claims=[_claim("audit", "10Gi")])
    yield _service("audit", PIPELINE, [("http", 8099)])


def manifests(nodes=3, storage_nodes=None, keycloak_url="http://nda-keycloak:8180", built=None):
    image = images(built)
    storage_nodes = nodes if storage_nodes is None else storage_nodes
    for group in (base(),
                  data_plane(image, nodes, storage_nodes),
                  catalog_and_identity(image, nodes, keycloak_url),
                  trino(image, nodes),
                  pipeline(image, nodes),
                  application(image, nodes)):
        yield from group


def render(nodes, storage_nodes, keycloak_url, out=None, built=None):
    import yaml

    class Dumper(yaml.SafeDumper):
        """Quote any string YAML would otherwise read back as something else.

        PyYAML implements YAML 1.1, where a bare `Y` is boolean true - so
        ACCEPT_EULA="Y" was emitted as `value: Y` and reached the API server as a
        bool, which it rejects. The same trap catches `on`, `off`, `no`, and
        anything that looks numeric like a version or a port.
        """

    # PyYAML and Go disagree about which bare scalars are booleans. PyYAML's own
    # resolver does NOT treat a lone `Y` as one, so asking it what needs quoting
    # leaves `value: Y` bare - and kubectl's Go parser then reads it as `true`
    # and the API server rejects the manifest. So the ambiguous set is spelled
    # out here as the WIDER of the two, not inferred from the writer.
    ambiguous = re.compile(
        r"^(y|Y|yes|Yes|YES|n|N|no|No|NO|true|True|TRUE|false|False|FALSE"
        r"|on|On|ON|off|Off|OFF|null|Null|NULL|~|)$")

    def _string(dumper, data):
        resolved = dumper.resolve(yaml.ScalarNode, data, (True, False))
        style = "'" if (resolved != "tag:yaml.org,2002:str"
                        or ambiguous.match(data)) else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)

    Dumper.add_representer(str, _string)
    documents = list(manifests(nodes, storage_nodes, keycloak_url, built))
    text = yaml.dump_all(documents, Dumper=Dumper, sort_keys=False,
                         default_flow_style=False)
    if out:
        pathlib.Path(out).write_text(text, encoding="utf-8")
        counts = {}
        for d in documents:
            counts[d["kind"]] = counts.get(d["kind"], 0) + 1
        print(f"Wrote {len(documents)} resources to {out}")
        for kind, n in sorted(counts.items()):
            print(f"  {kind:22} {n}")
    else:
        sys.stdout.write(text)
    return documents


def main():
    parser = argparse.ArgumentParser(description="Generate the Kubernetes manifests")
    parser.add_argument("action", choices=["render"])
    parser.add_argument("--nodes", type=int, default=3, help="Cluster size")
    parser.add_argument("--storage-nodes", type=int, default=None,
                        help="Nodes labelled nda.io/storage (default: all)")
    parser.add_argument("--keycloak-url", default="http://nda-keycloak:8180")
    parser.add_argument("-o", "--out", default=None)
    args = parser.parse_args()
    render(args.nodes, args.storage_nodes, args.keycloak_url, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
