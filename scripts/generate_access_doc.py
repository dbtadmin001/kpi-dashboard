"""Regenerate docs/ACCESS.md from the running environment.

Credentials are read from streaming/.env and from container environments, and written
straight to the file. They are never printed, so running this in a shared terminal or
pasting its output into a ticket does not leak anything.

    python scripts/generate_access_doc.py
"""
import datetime
import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "ACCESS.md"


def container_env(container, keys):
    result = subprocess.run(["docker", "inspect", container, "--format", "{{json .Config.Env}}"],
                            capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    found = {}
    for entry in json.loads(result.stdout):
        key, _, value = entry.partition("=")
        if key in keys:
            found[key] = value
    return found


def dotenv():
    path = ROOT / "streaming" / ".env"
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def main():
    env = dotenv()
    airflow = container_env("airflow-init", {"AIRFLOW_ADMIN_USERNAME", "AIRFLOW_ADMIN_PASSWORD",
                                             "_AIRFLOW_WWW_USER_USERNAME", "_AIRFLOW_WWW_USER_PASSWORD"})
    postgres = container_env("postgres", {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"})
    minio = container_env("minio", {"MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"})
    stakeholders = {}
    generated = ROOT / "infra" / "generated" / "credentials.json"
    if generated.exists():
        stakeholders = json.loads(generated.read_text(encoding="utf-8"))

    def value(source, key, fallback="(not set)"):
        return source.get(key) or fallback

    sa = value(env, "MSSQL_SA_PASSWORD")
    sim = value(env, "SQLSERVER_PASSWORD")
    dbz = value(env, "DEBEZIUM_PASSWORD")
    era = value(env, "ERASURE_PASSWORD")
    api = value(env, "NDA_API_KEY")
    af_user = value(airflow, "AIRFLOW_ADMIN_USERNAME", value(airflow, "_AIRFLOW_WWW_USER_USERNAME", "admin"))
    af_pass = value(airflow, "AIRFLOW_ADMIN_PASSWORD", value(airflow, "_AIRFLOW_WWW_USER_PASSWORD"))
    s3_key = value(minio, "MINIO_ROOT_USER", value(env, "AWS_ACCESS_KEY_ID"))
    s3_secret = value(minio, "MINIO_ROOT_PASSWORD", value(env, "AWS_SECRET_ACCESS_KEY"))
    pg_db = value(postgres, "POSTGRES_DB", "prod_db")
    pg_user = value(postgres, "POSTGRES_USER", "airflow")
    pg_pass = value(postgres, "POSTGRES_PASSWORD")
    kc_admin = value(env, "KEYCLOAK_ADMIN", "admin")
    kc_pass = value(env, "KEYCLOAK_ADMIN_PASSWORD")
    if stakeholders.get("users"):
        rows = ["#### Dashboard stakeholder accounts", "",
                "| Username | Password | Group | Sees | Can export | Trino role |",
                "|---|---|---|---|---|---|"]
        # Derived from the generated OPA document rather than restated here, so a
        # new group appears in this table without anyone remembering to add it.
        summary = {}
        try:
            entitlements = json.loads(
                (ROOT / "infra" / "generated" / "data.json").read_text(encoding="utf-8"))["entitlements"]
        except (OSError, ValueError, KeyError):
            entitlements = {}
        for group, rights in entitlements.items():
            indicators = ("all indicators" if rights.get("indicators") == ["*"]
                          else f"{len(rights.get('indicators', []))} indicators")
            processes = ", ".join(rights.get("processes", [])) or "-"
            exports = ", ".join(layer.replace("nda_", "") for layer in rights.get("layers", [])) or "nothing"
            summary[group] = (f"{processes} - {indicators}", exports, rights.get("trino_role", "-"))
        for name in sorted(stakeholders["users"]):
            user = stakeholders["users"][name]
            sees, exports, role = summary.get(user["group"], ("-", "-", "-"))
            rows.append(f"| `{name}` | `{user['password']}` | `{user['group']}` | {sees} | {exports} | `{role}` |")
        rows += ["", f"Client `{stakeholders.get('client_id')}` secret: `{stakeholders.get('client_secret')}`"]
        stakeholder_table = chr(10).join(rows)
    else:
        stakeholder_table = "_Run `terraform apply` in `infra/` to create the stakeholder accounts._"
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    doc = f"""# Service access reference

Generated {now} from the running environment by `scripts/generate_access_doc.py`.

> **This file holds real credentials and is gitignored.** Do not commit it or paste it into
> a ticket. Everything here is a local development deployment bound to `127.0.0.1` or the
> Docker bridge, and none of it is hardened for anything else.

## Web interfaces

| Service | URL | Sign in | Notes |
|---|---|---|---|
| **NDA dashboard** | http://127.0.0.1:8501 | none | Streamlit; the live KPI dashboard |
| **NDA serving API** | http://127.0.0.1:8095 | `X-API-Key` header | `/v1/dashboard`, `/health/ready` |
| **Flink** | http://127.0.0.1:8084 | none | JobManager UI; the medallion job runs here |
| **Debezium Connect** | http://127.0.0.1:8093 | none | REST only, no UI — try `/connectors` |
| **Trino** | http://127.0.0.1:8083 | username, no password | Query UI and REST |
| **OpenMetadata** | http://127.0.0.1:8585 | see below | Catalog, lineage, GDPR tags |
| **Marquez** | http://127.0.0.1:3000 | none | OpenLineage graph UI (API on `:5000`) |
| **Airflow** | http://127.0.0.1:8080 | see below | Webserver; scheduler has no port |
| **MinIO console** | http://127.0.0.1:9001 | see below | Object store (S3 API on `:9000`) |
| **Spark master** | http://127.0.0.1:8081 | none | Worker is currently **stopped** |
| **Iceberg REST** | http://127.0.0.1:8181 | none | Catalog API, no UI |
| **dbt docs** | http://127.0.0.1:8086 | none | Belongs to the atc-poc project |
| **OpenSearch** | http://127.0.0.1:9200 | none | Backs OpenMetadata search |
| **Keycloak** | http://127.0.0.1:8180 | see below | Identity for dashboard stakeholders |
| **OPA** | http://127.0.0.1:8182 | none | Policy decisions; REST only, no UI |

Kafka is not a web service: brokers are on `127.0.0.1:19092` from the host and
`nda-kafka:9092` inside the Docker network.

## Credentials

### SQL Server — the transactional system of record

Host `127.0.0.1,14333` (in-network `nda-sqlserver:1433`), database `NDAStreaming`.
Four principals, separated by privilege on purpose:

| Login | Password | Rights |
|---|---|---|
| `sa` | `{sa}` | sysadmin — provisioning only |
| `nda_simulator` | `{sim}` | SELECT/INSERT/UPDATE on the nine tables. **No DELETE.** |
| `nda_debezium` | `{dbz}` | SELECT plus the CDC reader role. Read-only. |
| `nda_erasure` | `{era}` | SELECT and DELETE. GDPR Art. 17 erasure only. |

### NDA serving API

    X-API-Key: {api}

### OpenMetadata

| User | Password |
|---|---|
| `admin@open-metadata.org` | `admin` |

Default basic auth — **change this before the stack leaves your machine.** The REST API
wants a JWT, obtained by logging in with a base64-encoded password (`admin` is `YWRtaW4=`).

### Airflow

| User | Password |
|---|---|
| `{af_user}` | `{af_pass}` |

A second admin, `platform_admin`, also exists. Airflow belongs to the atc-poc project; the
NDA operations DAG is written but **not installed**.

### MinIO / S3

| Access key | Secret key |
|---|---|
| `{s3_key}` | `{s3_secret}` |

These are **root** credentials, shared with Trino and Flink through `AWS_ACCESS_KEY_ID`
and `AWS_SECRET_ACCESS_KEY`. Replacing them with a service account scoped to the NDA
warehouse prefix is an open item.

### Keycloak (stakeholder identity)

| User | Password | Scope |
|---|---|---|
| `{kc_admin}` | `{kc_pass}` | Realm admin, `master` realm |

The `nda` realm, its **groups** and the policy attached to them are created by
Terraform in `infra/` (`./infra/run.sh apply`). Never click a group or its
`trino_role` into existence — edit `infra/variables.tf` and re-apply, or the
policy and the identity drift apart.

Individual **users** are the opposite: add them in the console and put them in a
group. Nothing else grants access, in the dashboard or in the warehouse. The
accounts below are seeds created by Terraform so the environment is usable on
first boot; joiners after that do not belong in a `.tf` file.

{stakeholder_table}

### Postgres (Airflow metadata, atc-poc)

| Host | Database | User | Password |
|---|---|---|---|
| `127.0.0.1:5432` | `{pg_db}` | `{pg_user}` | `{pg_pass}` |

### Services with no authentication

Kafka/Redpanda, Flink, Debezium Connect, Iceberg REST, Marquez and Spark all accept
unauthenticated local connections. Trino takes a username with no password — a username
alone is **not** authorization.

## Common commands

Load the environment first; Compose's `--env-file` does not export into your shell.

```bash
set -a && . ./streaming/.env && set +a
```

```bash
# Trino shell
docker exec -it trino trino --catalog iceberg --schema nda_gold

# Kafka topics and offsets
docker exec nda-streaming-kafka-1 rpk topic list
docker exec nda-streaming-kafka-1 rpk topic describe -p nda.NDAStreaming.dbo.ma_applications

# Debezium connector health
curl -s http://127.0.0.1:8093/connectors/nda-sqlserver/status

# Flink — jobs do NOT survive a restart, so resubmit after one
curl -s http://127.0.0.1:8084/jobs/overview
docker exec nda-streaming-jobmanager-1 ./bin/sql-client.sh -f /opt/flink/sql/03-medallion.sql

# Simulator: backfill to now, then follow real time
python -m streaming.simulator --count 0 --as-of "$(date +%Y-%m-%dT%H:%M:%S)" --follow

# Submit one application through the intake contract
python -m streaming.submission --process MA --type new --entity-id entity-001

# RBAC (Terraform is the source of truth for identity AND policy)
cd infra && ../.tools/terraform.exe apply -auto-approve
docker restart nda-streaming-opa-1     # OPA reloads its bundle on start

# Governance
python -m streaming.governance audit
python -m streaming.governance classify
python -m streaming.governance access --entity-id ENTITY
python -m streaming.governance erase --entity-id ENTITY --confirm

# Lineage
python -m streaming.lineage --emit           # OpenLineage to Marquez
python -m streaming.lineage --openmetadata   # the same graph into OpenMetadata

# Async enrichment
python -m streaming.enrichment --batch 400 --concurrency 24
```

## Starting from cold

1. Start Docker Desktop. The VM is capped at 21.5 GB by `~/.wslconfig`.
2. `docker start minio iceberg-rest`, then `docker start trino`.
3. NDA containers come back on their own (`restart: unless-stopped`).
4. **Resubmit the Flink job.** It is lost on every restart and nothing warns you.
5. Start the host processes: simulator follower, API on `:8095`, dashboard on `:8501`.
6. Optional: `docker start openmetadata-db openmetadata-search openmetadata-server openmetadata-ingestion`.
"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc, encoding="utf-8")
    resolved = {"airflow": af_pass != "(not set)", "minio": s3_secret != "(not set)",
                "postgres": pg_pass != "(not set)",
                "sqlserver": all(v != "(not set)" for v in (sa, sim, dbz, era)),
                "api_key": api != "(not set)"}
    print(f"Wrote {OUT.relative_to(ROOT)} ({len(doc.splitlines())} lines)")
    for name, ok in resolved.items():
        print(f"  {name:10} {'resolved' if ok else 'MISSING - check the source'}")


if __name__ == "__main__":
    main()
