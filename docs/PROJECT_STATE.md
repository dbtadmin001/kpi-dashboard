# Project state

Where the NDA regulatory data platform stands, what works, and what does not.
Last updated 14 September 2026.

**Everything is synthetic.** SLA targets are simulation settings, not agreed
service standards, and no figure here should be published without regulatory
sign-off.

---

## What this is

A regulatory performance platform for the National Drug Authority covering three
processes — Marketing Authorization (MA), Clinical Trials (CT) and Manufacturing
Quality (GMP). Application data is captured from a SQL Server system of record
by change data capture, projected through a streaming medallion lakehouse, and
served to a role-aware dashboard.

The pipeline end to end:

```
SQL Server → Debezium CDC → Kafka → Flink → Iceberg (bronze/silver/gold)
           → Trino → serving API → Streamlit dashboard
```

## Documentation

| Document | Covers |
|---|---|
| [architecture.html](architecture.html) | Full architecture report — stack, CDC, producer/processor sync, lineage, GDPR, RBAC, multi-pipeline reuse, streaming vs batch |
| [CATALOG_SEARCH.md](CATALOG_SEARCH.md) | How business users find data in OpenMetadata, and why descriptions are written that way |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Handoff runbook for the VM cluster — Ansible + Kubernetes |
| [ACCESS.md](ACCESS.md) | Service URLs and credentials (**gitignored**, generated) |

See also [SEMANTIC_LAYER.md](SEMANTIC_LAYER.md) for the metric definitions,
how they are generated and how they inherit the access rules.

## Code

| Module | Responsibility |
|---|---|
| `streaming/contracts.py` | KPI definitions, CDC column contract, business vocabulary. **Single source of truth** |
| `streaming/simulator.py` | Seeded application lifecycles written to SQL Server |
| `streaming/submission.py` | Real application intake with cross-process duplicate-ID rejection |
| `streaming/generate.py` | Generates source DDL, Debezium config and the Flink medallion SQL |
| `streaming/bootstrap.py` | Provisions the database, connector and gold views |
| `streaming/serving.py` / `api.py` | Dashboard contract and the authenticated serving API |
| `streaming/access.py` | Keycloak authentication, OPA authorization |
| `streaming/governance.py` | GDPR classification, masking, retention, subject access, erasure |
| `streaming/lineage.py` | OpenLineage graph, published to both Marquez and OpenMetadata |
| `streaming/catalog_docs.py` | Business descriptions and glossary for catalog search |
| `streaming/enrichment.py` | Asynchronous out-of-band entity enrichment |
| `analytics_answers.py` | Question-led analytics for the Reports tab |
| `marketplace/products.py` | The certified data products and the roles that may see them |
| `marketplace/identity.py` | Resolves Keycloak group membership into Trino's group file and provisions sandboxes |
| `marketplace/auth.py` | Token exchange, trusted TLS connections, `diagnose` |
| `marketplace/tls.py` | Development CA, server keystore and the JVM trust store |
| `marketplace/build.py` | Generates views, access rules and semantic models from the products |
| `marketplace/trino_config.py` | The governed coordinator, and the assertions that prove it bites |
| `infra/` | Terraform (identity + policy), Ansible (VM config), Kubernetes manifests |

**51 tests** cover the simulator, aggregation contract, lineage connectivity,
GDPR classification, access redaction, async enrichment, catalog descriptions and
the marketplace governance model, authentication and TLS material.

---

## What is running now

21 containers on this machine (dev environment). Source: **639 applications**
across the three processes, spanning 2025-04-01 to the present.

| Layer | State |
|---|---|
| **Source** | SQL Server with CDC on 9 tables, four separated principals |
| **Capture** | Debezium connector RUNNING, 9 topics at exact parity with source |
| **Processing** | Flink medallion job, 57 operators, checkpointing every 60s |
| **Lakehouse** | Iceberg `nda_bronze` / `nda_silver` / `nda_gold`, 0 quarantined rows |
| **Serving** | Trino gold views; FastAPI on `:8095`; Streamlit on `:8501` |
| **Catalog** | OpenMetadata — 67 entities, 54 lineage edges, 39 tables described |
| **Lineage** | Marquez — 42 jobs, 60 datasets, column-level facets |
| **Identity** | Keycloak realm `nda`, 8 groups, 14 accounts, all Terraform-managed |
| **Policy** | OPA (dashboard) and Trino file rules (warehouse), both from the same Terraform declaration |
| **Marketplace** | Governed Trino on `:8443` (TLS, OAuth2/JWT via Keycloak); 5 certified products; 10/10 RBAC assertions |

### The headline result

Trino's gold KPIs reconcile **exactly** against an independent Python
recomputation straight from SQL Server: **258 series, zero differences** in
value, numerator or denominator. Re-verified after a full Docker restart and
after a delete-and-reinsert cycle.

---

## What works, verified

- **CDC captures every state transition** — `RECEIVED → IN_REVIEW → COMPLETED` —
  which is what makes touch-time vs wait-time analysis possible at all. An hourly
  batch would see only the final state.
- **Idempotent replay.** Re-delivery converges: monotonic revisions at the
  source, upsert keys in the lake.
- **GDPR erasure end to end.** 32 source rows deleted, all 64 lake rows gone
  within 30 seconds via CDC. Snapshot expiry then makes time travel forget.
- **Stakeholder RBAC.** Nine accounts across five groups. An MA analyst is
  refused bronze and refused CT gold; the public account is refused every export.
  Enforced at the API, not the UI.
- **Question-led analytics.** Five regulatory questions answered in words first —
  including *"35% of elapsed time is spent waiting, not being worked on."*
- **Catalog search.** 12/12 lay-language queries return the correct gold table.
- **Async enrichment.** 321 entities in 11.6s at concurrency 24; failures
  recorded as `unavailable` rather than dropped; reports LEFT JOIN so a pending
  lookup never blocks.

---

## Open gaps, most serious first

| Gap | Impact |
|---|---|
| **Flink job needs manual resubmission** | Happened twice today. Nothing alerts; the stack looks healthy while no data flows. Needs the Flink Kubernetes Operator — a supervision loop, not a schedule |
| **Single Kafka broker, RF=1** | Broker loss means data loss inside the retention window |
| **MinIO root credentials in `.env`** | Full object-store access, not scoped to this platform |
| **Cluster deployment untested** | Manifests and playbook validate, but k3s install, node join and MinIO erasure-set formation have never run against real VMs |
| **Kafka/Connect/Flink sit in the pipeline project** | They are shared-runtime services and should move to the platform project *before* a second pipeline, not after |
| **Catalog ingestion is manual** | Descriptions and lineage drift as schemas change; belongs on Airflow |
| **Keycloak uses `start-dev` + H2** | Fine for a demo, not for anything else |
| **No backups** | MinIO holds the lakehouse and nothing copies it elsewhere |
| **7-day Kafka retention** | Bounds how far the lake can be rebuilt without re-snapshotting |
| **SLA targets are invented** | Every threshold needs regulatory sign-off before publication |

---

## Decisions worth remembering

**Streaming was not chosen for latency.** Turnaround times are measured in weeks;
nobody acts differently on a 45-second number versus a 45-minute one. CDC earns
its place because it captures every state transition, which is what makes
`touch_days` / `wait_days` bottleneck analysis possible. Drop that requirement
and hourly batch is the correct choice, removing roughly two-thirds of the
moving parts.

**Identity and authorization are deliberately separate.** Keycloak answers *who
you are*; OPA answers *what that allows* in the dashboard, and Trino's file rules
answer it in the warehouse. All three are generated from one Terraform map, so a
group cannot exist without a policy or vice versa.

**Access is granted to groups, never to people.** A Keycloak group carries its
warehouse role as the `trino_role` attribute; `marketplace/identity.py` projects
membership into the file Trino reads, on a 30-second refresh. Adding a joiner is
a console action with no code change, and the only username that appears in any
rule is the one naming a personal sandbox schema — derived from membership, never
authored. Trino has no OIDC group provider, so projection is the alternative to
putting Keycloak on the hot path of every query.

**People use single sign-on; only service accounts hold tokens.** A person
connects with `externalAuthentication=true` and signs in at Keycloak as
themselves - identical settings for everyone, nothing issued per user, so
onboarding is adding them to a group and nothing else. Client machines trust the
platform CA through configuration management, not through each person being
handed a file.

**Authentication and authorisation are answered by different things, on purpose.**
Keycloak proves who you are and Trino decides what that may do. Trino takes the
principal from the token's `preferred_username` claim and resolves it to a role
through the group file, so a token cannot assert a role and a group file cannot
assert an identity. No impersonation rules exist, so a session may only run as
its own principal.

**A directory outage freezes membership rather than falling back.** The sync
refuses to write and Trino keeps the last file it read. Falling back to a list in
code would let a leaver keep working; writing an empty file would revoke the view
owners mid-query and break every certified view.

**The public sees timeliness, never compliance.** `pct_facilities_compliant` and
the CAPA measures report failure rates over small cohorts of named facilities —
a re-identification risk the timeliness indicators do not carry.

**Nothing writes to the lake directly.** The transactional database is the only
writable surface, which is what makes the lake reproducible. It was rebuilt from
source three times during development.

**No pipeline builds its own images.** This one builds zero: it runs upstream
tags and shares MinIO, Trino, the Iceberg catalog, Marquez and OpenMetadata over
a shared Docker network. A second pipeline should cost one container — its own
source database — and no image builds.

---

## Running it from cold

```bash
# 1. Infrastructure
docker start minio iceberg-rest && docker start trino
cd streaming && docker compose up -d

# 2. Resubmit the Flink job (it does NOT survive a restart)
docker exec nda-streaming-jobmanager-1 ./bin/sql-client.sh -f /opt/flink/sql/03-medallion.sql

# 3. Host processes
set -a && . ./streaming/.env && set +a
python -m streaming.simulator --count 0 --as-of "$(date +%Y-%m-%dT%H:%M:%S)" --follow &
python -m uvicorn streaming.api:app --host 127.0.0.1 --port 8095 &
python -m streamlit run stream_kpi_dash_g2.py --server.port 8501

# 4. Verify
python -m pytest streaming/tests -q
```

Full service list and credentials: [ACCESS.md](ACCESS.md).
Deploying to VMs: [DEPLOYMENT.md](DEPLOYMENT.md).
