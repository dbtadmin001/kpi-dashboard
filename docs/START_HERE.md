# Picking this up on another machine

Everything in git is the code. None of the credentials, certificates or collected
state travel with it, deliberately — so a fresh clone runs nothing until you
regenerate them. This is that list, in order.

---

## What is NOT in the repository

Six things exist only on the machine that made them. A clone has none of them,
and that is the point: they are secrets, or they are derived and cheap to rebuild.

| Missing | What it is | How to get it back |
|---|---|---|
| `streaming/.env` | 31 settings incl. every password | Copy from `streaming/.env.example` and fill in, or copy the real file across by hand |
| `infra/generated/credentials.json` | Seed account passwords | `./infra/run.sh apply` regenerates them |
| `infra/generated/trino_oidc.json` | Trino's OIDC client secret | same |
| `marketplace/generated/trino/tls/*` | Development CA, server keystore, JVM trust store | `python -m marketplace.tls issue` |
| `catalog/catalog.db` | The collected infrastructure graph | `python -m catalog.cli collect` |
| `docs/ACCESS.md` | Generated credential reference | `python scripts/generate_access_doc.py` |

`streaming/.env` is the only one you cannot regenerate from the repo alone,
because it holds the passwords the running containers already use. Move it
across securely, or rebuild the stack from empty volumes with new secrets.

---

## 1. Clone and install

```bash
git clone https://github.com/dbtadmin001/kpi-dashboard.git
cd kpi-dashboard
pip install --require-hashes -r delivery/requirements.lock
```

The lock is hash-pinned, so a substituted package fails the install rather than
the review. It needs Python 3.11.

## 2. Check the tree is coherent before starting anything

```bash
export PYTHONPATH="$PWD"
python -m delivery.validate      # 33 checks, no services required
python -m pytest -q              # 72 tests
```

Both pass on a clean clone. If they do not, fix that before starting containers —
these run in seconds and every failure they catch is one you would otherwise meet
in a five-minute Docker startup.

## 3. Bring up the platform

```bash
cp streaming/.env.example streaming/.env      # then fill it in
set -a && . ./streaming/.env && set +a

docker compose -f streaming/compose.yaml up -d
./infra/run.sh apply                          # Keycloak realm, groups, OIDC client
python -m marketplace.tls issue               # CA + certificates
python -m marketplace.tls trust --apply       # trust the CA on this machine
python -m marketplace.trino_config up         # governed Trino on :8443
python -m marketplace.identity sync           # groups, rules and sandboxes
python -m marketplace.trino_config check      # 10 RBAC assertions must pass
```

**The Flink job does not survive a restart and nothing alerts on it.** The stack
will look healthy with no data flowing. Resubmit it after the stack is up.

## 4. Bring up the catalog

```bash
python -m catalog.cli collect     # reads every provider, ~30s
python -m catalog.cli serve       # http://127.0.0.1:8600
```

Collection is read-only. It cannot change anything it looks at.

---

## Where things run

| | |
|---|---|
| Infrastructure catalog | http://127.0.0.1:8600 |
| Governed Trino | https://127.0.0.1:8443 (TLS, Keycloak sign-in) |
| Keycloak | http://localhost:8180 |
| KPI dashboard | http://localhost:8501 |
| OpenMetadata | http://localhost:8585 |
| MinIO | http://127.0.0.1:9000 |

---

## Branches

| Branch | Meaning | On push |
|---|---|---|
| `master` | Integration. Work lands here and is tested | validate + integration, nothing published |
| `production` | What may be deployed | validate + integration + publish |

Work on `master`. Reaching `production` is a reviewed pull request, and that is
the only branch that publishes images. Add `[skip ci]` to the tip commit message
to push without triggering a run.

Deployment is never a merge. An artifact is built once and promoted:

```bash
python -m delivery.promote status
python -m delivery.promote verify <artifact> --env staging
python -m delivery.promote promote <artifact> --to production
```

---

## Known state, honestly

- **The integration gate is a real disposable stack.** It starts isolated
  PostgreSQL, SQL Server, Kafka/Connect, Flink, Iceberg, MinIO, Keycloak,
  Trino, OPA and audit services. Bootstrap renders OIDC/TLS/policy material
  before the serving plane starts; `flink-sql` submits the StatementSet; then
  the gate writes deterministic source data and checks CDC, RBAC, browser SSO,
  dbt metrics and audit lineage. The first GitHub run after this change is the
  acceptance record for the runner itself; it must pass before deployment.
- **`__write_probe` is still the repository's default branch.** Change it to
  `master` in Settings → General, then `git push origin --delete __write_probe`.
  It exists because a write test created the first branch on an empty repo.
- **The Kubernetes manifests have never been applied.** They are generated and
  40/40 validate against a real k3s API server, but no cluster has run them.
- **OpenMetadata is not in the catalog.** Its ingestion token has expired, so it
  currently reports no services; a collector would add an empty box.
- **Branch protection is not set.** `python -m delivery.github protect` applies
  it once you are signed in with `gh`.

---

## The three things worth knowing before you change anything

**Access is granted to groups, never to people.** Keycloak is the directory;
`marketplace/identity.py` projects membership into the file Trino reads. The only
username in any access rule is the one naming a personal sandbox, and that row is
derived from membership rather than written by hand. A test enforces this.

**The catalog never writes.** One non-GET route exists and it triggers a read.
A test enumerates every route and fails the build if a mutating verb appears.

**A rebuild is a different artifact.** Promotion moves the exact images that were
tested; nothing is rebuilt per environment. Production refuses a tag because a tag
can be moved underneath you.
