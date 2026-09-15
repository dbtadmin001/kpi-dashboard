# Running the data marketplace

For platform administrators and data engineers. Covers operating it, changing
it, and the things that will bite you.

---

## The two principles

**1. Policy is generated from `marketplace/products.py`.** That file is the
source of truth for the certified views, the shape of the Trino access rules, the
dbt semantic models and the catalogue entries. Editing any generated artefact by
hand means it is overwritten on the next run, and the four surfaces drift apart.

**2. Membership comes from Keycloak, and only from Keycloak.** Rules are written
against *groups*; the directory decides who is in them. No file in this repository
says who is an analyst.

```
                       WHAT a role may do          WHO holds the role
                    ┌───────────────────────┐   ┌──────────────────────┐
products.py ────────┤ certified Trino views │   │  Keycloak realm nda  │
                    │ access rules shape    │   │  groups + membership │
infra/variables.tf ─┤ OPA entitlements      │   └──────────┬───────────┘
  (trino_role)      │ dbt semantic models   │              │
                    │ OpenMetadata docs     │   marketplace/identity.py
                    └───────────┬───────────┘              │
                                └──────────┬───────────────┘
                                           ▼
                            rules.json  +  groups.txt
                          (Trino re-reads both every 30s)
```

Adding a person touches only the right-hand box. Adding a *capability* touches
only the left. They meet in the generated config and nowhere else.

## Components

| Component | Where | Purpose |
|---|---|---|
| Keycloak | `nda-streaming-keycloak-1`, port **8180** | The directory. Users, groups, `trino_role` |
| Marketplace Trino | `trino-marketplace`, port **8443** (TLS) | The governed door. Rules enforced here |
| Group sync | `marketplace/identity.py` | Keycloak → `groups.txt`, every 30s |
| OPA | `nda-streaming-opa-1`, port **8182** | Dashboard entitlements from the same groups |
| Shared Trino | `trino`, port 8083 | Another project's, in another repo. **Not ours, not governed, not used** |
| Audit collector | host, port **8099** | Receives Trino query events |
| Audit table | `iceberg.marketplace_audit.query_log` | Queryable query log |
| dbt project | `marketplace/dbt` | Owns the views and the semantic layer - see [SEMANTIC_LAYER.md](SEMANTIC_LAYER.md) |

The marketplace has **its own coordinator** because Trino's access control is
engine-wide — enabling rules on the shared cluster would govern the other
project's catalogs too. Both point at the same Iceberg catalog and object store,
so no data is duplicated.

## Authentication

The coordinator requires a token. It did not always: until recently it accepted
whatever username a client sent, which meant `--user admin` worked for anyone and
DBeaver silently connected as the operator's OS account. The rules were sound and
the identity under them was a free-text field.

| Caller | Flow | How |
|---|---|---|
| A person | OAuth2 browser redirect | `SSL=true SSLUseSystemTrustStore=true externalAuthentication=true`. Same settings for everyone |
| A program (dbt, notebook, CI, our services) | JWT bearer | `eval "$(python -m marketplace.auth env <service-account>)"` |

**Onboarding is zero-touch.** Put the person in a Keycloak group; they connect
with the same settings as everyone else and sign in as themselves. Nothing is
issued per user — no token, no trust store file, no connection built for them.
If you find yourself running a command per person, something upstream is wrong.

The one prerequisite is that client machines trust the platform CA. That is a
fleet concern, handled by configuration management:

```bash
./infra/ansible/run.sh site.yml --tags ca_trust   # managed Linux hosts
python -m marketplace.tls trust --apply           # a single workstation
```

Bearer tokens remain correct for **service accounts** — dbt, scheduled jobs, CI —
which cannot open a browser. Handing tokens to people is the fallback for a
machine where the CA cannot be installed, and it does not scale: tokens expire
and somebody has to reissue them.

Both flows validate against realm `nda` and both take the Trino principal from
the token's `preferred_username` claim, so they cannot disagree about who you
are. A session may only run as its own principal unless an impersonation rule
says otherwise — Trino calls `checkCanImpersonateUser` when the requested user
differs, and the rules deny that for everyone by default. See
[Several accounts on one machine](#several-accounts-on-one-machine).

**On the token path only, the session username must equal the token principal.**
Trino takes the session user from the client's username field and falls back to
the client's OS account when it is blank, which then fails as impersonation. The
browser flow has no such trap: the driver sets both from the same sign-in.

Token lifetime is set on the `trino` client (`trino_token_lifespan_seconds`,
default 12h) but the realm's 8h SSO session cap binds first, so the real answer
is 8h — read off the token rather than asserted.

```bash
python -m marketplace.tls issue                    # CA + server certificate
./infra/run.sh apply                               # creates the `trino` OIDC client
python -m marketplace.trino_config up              # restarts with TLS + OAUTH2,JWT
python -m marketplace.auth diagnose alice.nakato   # prove it end to end
```

**Ports change when authentication comes on.** `trino_config up` publishes
**8443 (HTTPS)** and stops publishing 8090, because an open unauthenticated port
beside a locked one is not a smaller version of the problem — it is the problem.
If you see 8090 still listening, the OIDC client has not been created and the
coordinator says so at startup.

### Five things that will bite you

**1. The issuer must be pinned.** Keycloak derives `iss` from the request unless
told otherwise, so a token fetched at `127.0.0.1:8180` and one fetched at
`nda-keycloak:8180` carry different issuers and Trino rejects whichever it was
not configured for. `compose.yaml` sets `--hostname` to fix the issuer and
`--hostname-backchannel-dynamic=true` so containers can still reach it internally.
Trino is configured with the public issuer but the **internal** JWKS and token
URLs — different address, same issuer, which is exactly what that flag is for.

**2. The audience must name Trino.** Without the audience mapper the token is
valid but "not for you". Trino is right to reject it and it is an unpleasant
thing to debug, because nothing in the message says *audience*.

**3. Never turn off certificate verification to make a client work.**
`verify=False` or `SSLVerification=NONE` will connect, and will also hand the
bearer token to anyone who answers on that address.

**4. There are two trust files and they are not interchangeable.**
The JVM cannot read a PEM as a trust store, so telling a DBeaver user to point
`SSLTrustStorePath` at the `.crt` is advice that does not work.

| File | Reader |
|---|---|
| `marketplace-truststore.p12` | DBeaver, Trino JDBC, anything on the JVM |
| `marketplace-ca.crt` | Python, dbt, curl |

The trust store is built with **keytool**, not with a PKCS12 library. Java only
trusts a certificate carrying its own `trustedKeyUsage` attribute, which no
general-purpose library emits; a library-written PKCS12 loads fine and reports
`0 entries`. `tls.py` asserts `trustedCertEntry` is present rather than assuming
it. Password: `python -m marketplace.tls show`.

**5. Publish both loopback families.**
`localhost` resolves to `::1` before `127.0.0.1`, and the JVM honours that order.
Binding only `127.0.0.1` gives Java clients
`Failed to connect to localhost/[0:0:0:0:0:0:0:1]:8443 ... Connection refused`
against an address the server never claimed — while Python and the CLI work,
because they fall back to IPv4. `trino_config.loopback()` publishes both.

### Rolling back

Delete `infra/generated/trino_oidc.json` and run `trino_config up`. The
coordinator restarts unauthenticated on 8090 with a warning. Everything in the
platform goes through `marketplace.auth.connect_any()`, which follows that state,
so nothing needs editing in either direction.

## Several accounts on one machine

Two different problems get confused here, and only one of them is about browsers.

### 1. One person, two accounts

Common in production: a normal account and a separate privileged one. The hazard
is not that it fails - it is that it can **succeed as the wrong identity**.
Keycloak keeps an SSO session (30 min idle, 8 h max), so the second connection is
answered from the first sign-in with no prompt.

| Connection has | Result |
|---|---|
| no `user=` | connects silently as the first account. **The dangerous case** |
| `user=alice.nakato` | fails with `cannot impersonate user` |

So the standing rule is: **always pin `user=` in the connection.** It converts a
silent wrong-identity into a loud error, and costs nothing.

Beyond that, give each account its own browser profile, or append `&prompt=login`
to force re-authentication - Keycloak honours it even with a live session. If
policy demands re-authentication every time, set the `trino` client's
**Authentication Max Age** in Keycloak rather than relying on people remembering.

### 2. Support needs to see what a user sees

This is the one that gets solved badly. Sharing a password destroys attribution;
juggling browser sessions is not auditable either. The governed mechanism is
**impersonation**.

```bash
export MARKETPLACE_ALLOW_IMPERSONATION=1
python -m marketplace.identity sync
```

An administrator then opens a session as the user, Trino enforces **the target's**
rules, and the audit records both identities.

```sql
SELECT event_time, principal, username, query_text
FROM iceberg.marketplace_audit.query_log
WHERE principal IS NOT NULL AND principal <> username
ORDER BY event_time DESC;
```

Four properties make this safe rather than a back door:

- **Off by default.** Without the flag, every impersonation rule is `allow: false`.
- **Granted only to `administrator`**, who can already read every table - so it
  adds no data access, only the ability to see it through someone else's
  permissions.
- **Platform principals can never be targets.** `marketplace_owner`,
  `nda_dashboard` and `admin` are excluded by the *first* rule, because Trino
  uses first match and becoming a view owner would hand over the privileges every
  certified view runs with.
- **The audit records the real principal.** This had to be added first: the
  collector previously stored only the effective user, so a support session would
  have been recorded as the person being viewed. Enabling impersonation without
  that would have been worse than not having it.

Originals are **derived from directory membership**, like sandbox rules - not
authored per person. Note that `original_role` in Trino's impersonation rules
means a *Trino* role, which this platform does not use; it would never match and
impersonation would be silently denied rather than visibly misconfigured.

### 3. What not to do

Do not set `externalAuthenticationTokenCache=MEMORY`. Its default is `NONE`.
`MEMORY` caches the token for the whole DBeaver process, so every connection
after the first reuses the first identity - across browsers, silently.

## Identity

```bash
python -m marketplace.identity show     # what Keycloak says, and what it resolves to
python -m marketplace.identity sync     # render groups.txt + rules.json
python -m marketplace.identity watch    # follow the directory continuously
```

`show` prints each group, the role it carries, its members, and then the resolved
role→user mapping Trino will act on.

**Trino does not talk to Keycloak directly.** Trino's file-based access control
resolves groups through a *group provider*, and there is no OIDC group provider —
groups come from a file, LDAP, or a custom plugin. So the directory is projected
into the file Trino already reads, on a 30-second refresh. The alternative, a
custom Java group provider calling the admin API on every query, puts Keycloak on
the hot path of every statement; this does not.

### What happens when Keycloak is down

The sync **refuses to write** and leaves the previous `groups.txt` in place.
Trino carries on with the last membership it read.

That is deliberate, and it is the only safe choice of three:

| Behaviour on outage | Result |
|---|---|
| Fall back to a list in code | Access silently diverges from the directory — a leaver keeps working |
| Write an empty file | Every user, including the view owners, loses access mid-query |
| **Freeze the last known good** | Membership goes stale and loud; nothing opens up |

An empty result from a reachable Keycloak is also treated as an outage, not as an
organisation with no staff — otherwise one misapplied Terraform run revokes
everyone.

### Platform identities

`marketplace_owner`, `nda_dashboard` and `admin` are engine identities, not
people. They are in Keycloak under `platform-services` so an auditor asking "who
is an administrator" has one place to look — **and** pinned in
`identity.PLATFORM_IDENTITIES`, because `nda_dashboard` owns the `nda_gold.all_*`
views that every certified view reads. If a directory hiccup dropped that user,
Trino's DEFINER check would fail the whole chain. The two copies agree; the code
copy exists so an outage cannot take the serving layer down with it.

## Daily operation

```bash
set -a && . ./streaming/.env && set +a     # always first; see the trap below

python -m marketplace.identity show        # who the directory says is in what
python -m marketplace.identity sync        # groups, rules and sandboxes from Keycloak
python -m marketplace.identity provision   # sandbox schemas only
python -m marketplace.trino_config up      # start/restart the coordinator
python -m marketplace.trino_config check   # 10 RBAC assertions
python -m marketplace.trino_config visible # what each role can see
python -m marketplace.audit serve          # run the audit collector
python -m marketplace.audit report         # who ran what
python -m marketplace.retention report     # sandbox usage and what is due to expire
```

### Health check after any change

```bash
python -m marketplace.trino_config check
```

Ten assertions covering every role boundary. If any fails, users can see
something they should not, or cannot see something they need.

## Changing the marketplace

### Add or edit a data product

1. Edit `PRODUCTS` in `marketplace/products.py` — SQL, dimensions, measures,
   description, owner.
2. Decide its audience in `PRODUCT_AUDIENCE`. **A product with no audience is
   invisible**, which is the safe default: forgetting to decide fails closed.
3. Apply:

```bash
python -m marketplace.build generate       # regenerate rules and semantic models
cd marketplace/dbt && dbt build            # create/update the views
python -m marketplace.trino_config write   # refresh rules (picked up within 30s)
python -m marketplace.build verify
```

### Add or move a user — in Keycloak, not in code

**There is no user list in this repository.** Keycloak is the directory; Trino
reads it. Adding someone is a console action:

1. Open **http://localhost:8180** → realm `nda` → **Users** → *Add user*.
2. **Groups** tab → *Join Group* → pick one (`ma-analysts`, `data-science`, …).
3. That is the whole grant. Within 30 seconds Trino sees it:

```bash
python -m marketplace.identity sync     # group file, access rules AND their sandbox
```

`sync` provisions the sandbox schema too. An access rule granting ownership of
`sandbox_new_person` does not conjure the schema, and without it their first
`CREATE VIEW` fails with "Schema does not exist" — which reads as a permissions
problem and is not one. Run `python -m marketplace.identity watch` (or schedule
`sync`) and even this step disappears.

### Their password

Keycloak holds it, and it is the only one — Trino keeps no password list and
never sees the password at all, only a token minted from it.

When you create the account, set a password and **leave "Temporary" on**.
Keycloak then forces a change at first sign-in, so the value you send them is
never the value they keep. Seed accounts are the exception: Terraform generates
theirs non-temporary so the environment is usable on first boot, and they are
listed in `docs/ACCESS.md` (gitignored).

There is nothing to distribute beyond that. DBeaver and the CLI never receive the
password — they open a browser, Keycloak authenticates, and the tool gets a
token. That is why the connection dialog has an empty password box and should
keep one.

To move someone, change their group. To revoke, remove the group or disable the
account; disabled users are dropped from the next sync, so suspension in the
directory *is* revocation in the warehouse. No access rule is edited either way.

### Roles

Policy attaches to the **group**. The group carries its warehouse role as the
`trino_role` attribute, set by Terraform, so identity and access are one
declaration rather than two lists someone has to keep in step.

| Keycloak group | `trino_role` | Marketplace | Physical schemas | Sandbox |
|---|---|---|---|---|
| `public` | `business_user` | 4 of 5 products | none — invisible | own, views and tables |
| `executive` | `business_user` | 4 of 5 products | none — invisible | own |
| `ma-analysts`, `ct-analysts`, `gmp-analysts` | `analyst` | all 5 | `nda_gold` read, **entity_id masked** | own |
| `data-science` | `data_scientist` | all 5 | `nda_gold`, `nda_silver` read, **entity_id masked** | own |
| `data-engineering` | `data_engineer` | all + authoring | all, read-write | own |
| `platform-services` | `administrator` | all + grants | all, read-write | all |

Several groups can share a role: the three analyst groups differ in which
*process* the dashboard shows them (OPA decides that), while all three get the
same warehouse slice. One directory entry, two enforcement points.

To add a role or re-map a group, edit `stakeholder_groups` in
[infra/variables.tf](../infra/variables.tf) and apply — that one map creates the
Keycloak group, renders the OPA entitlement, and sets the Trino role.

```bash
./infra/run.sh apply                # containerised; no local Terraform needed
python -m marketplace.identity sync
```

`trino_role` must name a role in `ROLES`. A typo does not fail quietly — the
sync refuses and says which group is wrong.

## Five traps, all of which cost me time

**1. Catalog-level `read-only` blocks sandbox writes.**
Setting `"allow": "read-only"` on the catalog prevents *every* write in it,
including a user creating a table in their own sandbox. Catalog access must be
`all`; the schema and table rules do the restricting. Production stays read-only
because its table rules grant `SELECT` and nothing else — verified by the check.

**2. View owners need `GRANT_SELECT`, not just `SELECT`.**
Without it every certified view fails with *"view owner does not have sufficient
privileges"*. That is Trino's DEFINER check working: the view runs as its owner,
so the owner must be allowed to hand the data on.

**3. Every owner in a view chain needs privileges.**
Our products read `nda_gold.all_*`, which are themselves views owned by the
platform's serving user (`nda_dashboard`). That user must be privileged too, or
the chain breaks one level down with a confusing error.

**4. Sandbox rules are the one place a username appears.**
The username-to-schema mapping replaces dots with underscores
(`alice.nakato` → `sandbox_alice_nakato`), and Trino's file rules have no
backreference that can express that, so the generator emits one rule per user.
Those rows are *derived from directory membership*, never authored — which is why
`identity sync` rewrites `rules.json` as well as `groups.txt`. A test asserts
that every other rule in the file grants to a group, so a hand-written personal
grant cannot creep back in.

**5. Load the environment before generating config.**
`trino_config` reads the MinIO credentials from the environment. It now refuses
to run without them — an earlier silent default produced a coordinator that
started cleanly and failed every query with *"failed to get status for file"*,
which points at storage when the fault is the config.

```bash
set -a && . ./streaming/.env && set +a
```

## Column masking

`entity_id` is classified as a pseudonymous identifier, so analysts and data
scientists reading the physical layer see it hashed; engineers and admins see it
raw. Verified:

```
alice.nakato  ->  sha256:4AC516B8B30A65E6E96514001E75398665DD38335BC48BCC37D4DAA6A4AB79E8
dana.okello   ->  c416075b-022c-5bff-ad24-231d8e9b3dcb
```

**The mask must sit on the same rule that grants SELECT.** Trino applies the
first matching table rule, so a masking rule added *after* the plain grant is
never reached — it fails silently, showing raw values while looking configured.

## Audit

Trino POSTs a completed-query event to the collector, which writes a durable
JSONL line **first** and then batches into Iceberg. Losing an audit record is
worse than a slow query, so the file is the record of truth and the table is a
queryable projection of it.

```sql
-- Who touched compliance data this week?
SELECT username, COUNT(*) AS queries, MAX(event_time) AS last_seen
FROM iceberg.marketplace_audit.query_log
WHERE tables_accessed LIKE '%quality_metrics%'
  AND event_time > CURRENT_TIMESTAMP - INTERVAL '7' DAY
GROUP BY username ORDER BY queries DESC;

-- Denied attempts: who tried to reach something they should not?
SELECT username, query_text, error_code, event_time
FROM iceberg.marketplace_audit.query_log
WHERE state = 'FAILED' AND error_code LIKE '%PERMISSION%'
ORDER BY event_time DESC LIMIT 50;
```

The log records the **physical tables actually read**, not just the view the user
named. A business user querying `marketplace.delay_analysis` produces an audit
row naming `nda_gold.fact_*` — they never saw those names, but the audit knows.

## Sandbox housekeeping

```bash
python -m marketplace.retention report            # preview; changes nothing
python -m marketplace.retention enforce --confirm # actually drop
```

Defaults: **30-day TTL** on untouched derived tables, **5 GB per sandbox**.
Override with `SANDBOX_TTL_DAYS` and `SANDBOX_QUOTA_MB`. Views are never dropped
— they cost about 600 bytes and recompute on read. Snapshots are expired before
the drop, otherwise the files linger.

Run it on a schedule. Without it, the near-zero storage promise holds only while
people are well behaved.

## Storage, measured

| Schema | Size | Objects |
|---|---|---|
| `marketplace` (5 views + time spine) | 53 KiB | 21 |
| `sandbox_alice_nakato` (1 view + 1 table) | 34 KiB | 7 |
| `marketplace_audit` | 893 KiB | 68 |
| `nda_gold` (the actual data) | 235 MiB | 11,722 |

The certified layer is **0.02%** of the data it exposes. The one table the
marketplace itself writes is MetricFlow's day-grain time spine (2,191 rows).

## Known gaps

- **The TLS certificate is self-signed.** Fine on a laptop, and clients trust the
  generated CA rather than skipping verification, but production needs a real
  certificate from your own CA. `marketplace/tls.py` is then deleted, not adapted.
- **Keycloak runs `start-dev` on H2.** The realm is reproducible from Terraform,
  so this is recoverable rather than dangerous, but it is not a database.
- **Tokens last 8 hours** (the realm's SSO session cap, below the 12h set on
  the `trino` client). Long dbt builds should fetch one immediately before
  running rather than reusing a stale export.
- **The group file is a projection, not a live lookup.** Membership changes take
  up to 30 seconds, and up to `--interval` seconds more if you rely on `watch`
  rather than syncing on change. Acceptable for joiners; worth knowing for
  urgent revocation, where disabling the Keycloak account and running
  `identity sync` immediately is the fast path.
- **Retention is manual** until it is scheduled.
- **The shared Trino on 8083 is out of scope.** Its config lives in another
  repository (`deng-workflows/...`), so this project does not govern it. Every
  service here goes through `marketplace.auth.connect_any()`, which targets the
  governed coordinator. If someone connects to 8083 by hand they bypass these
  rules — that
  is a people problem, not a config one, and the fix is to retire 8083 or move
  its catalogs behind the governed coordinator.
