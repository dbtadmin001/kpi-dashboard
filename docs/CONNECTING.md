# Connecting to the marketplace

Step by step, for DBeaver, VS Code and Jupyter.

Connection details, the same for all three:

| | |
|---|---|
| Host | `127.0.0.1` (**not** `localhost`) |
| Port | `8443` |
| Catalog | `iceberg` |
| Schema | `marketplace` |
| Username | your work account, e.g. `alice.nakato` |
| Password | none — you sign in to Keycloak, not to Trino |

---

## Step 0 — once per machine

Trust the platform certificate:

```bash
cd "<the kpi-dashboard folder>"
set -a && . ./streaming/.env && set +a
python -m marketplace.tls trust --apply
```

**On this machine this is already done.** To undo it later:
`certutil -delstore -user Root "NDA marketplace development CA"`

---

## A. DBeaver

DBeaver builds the JDBC URL from the Main tab, and **the Main tab has no SSL
option**. If you leave it at that, the driver talks plain HTTP to a TLS port and
you get:

```
java.io.IOException: unexpected end of stream on http://127.0.0.1:8443/...
\n not found: limit=7 content=15030300020250...
```

That `1503 0300` is the server's TLS alert being read as if it were text. The
fix is to put the settings in the URL, where you can see them.

### A1. Get a token

```bash
cd "<the kpi-dashboard folder>"
set -a && . ./streaming/.env && set +a
python -m marketplace.auth dbeaver alice.nakato --url
```

It prints one line starting `jdbc:trino://`. Copy it.

### A2. Create the connection

1. **Database** -> **New Database Connection** -> **Trino** -> **Next**
2. At the top of the dialog, find **Connect by:** and select **URL**
   (it defaults to **Host**). The Host/Port/Database boxes grey out and a
   single **URL** box appears.
3. Paste the URL from A1 into the **URL** box.
4. Leave **Username** and **Password** empty.
5. **Test Connection** -> should say Connected.
6. **Finish**.

### A3. Check it

Expand the connection in the Database Navigator:
`iceberg` -> `marketplace` -> five tables (`application_throughput`,
`delay_analysis`, `indicator_performance`, `quality_metrics`,
`turnaround_performance`).

SQL Editor -> **New SQL script**, paste, Ctrl+Enter:

```sql
SELECT process, workflow_stage, avg_waiting_days
FROM iceberg.marketplace.delay_analysis
WHERE process = 'GMP'
ORDER BY avg_waiting_days DESC
LIMIT 10;
```

### A4. When the token expires (8 hours)

Re-run A1, then in DBeaver: right-click the connection -> **Edit Connection** ->
replace the URL -> **OK**.

### A5. Browser sign-in instead of a token

Avoids the 8-hour refresh. Same steps, but use this URL in A2 (no token in it):

```
jdbc:trino://127.0.0.1:8443/iceberg/marketplace?SSL=true&SSLUseSystemTrustStore=true&externalAuthentication=true
```

On **Test Connection** a browser opens; sign in to Keycloak. DBeaver caches the
token and renews it by asking you again, so there is nothing to paste.

*Verified: the token URL in A1-A2 was tested end to end against this
coordinator using DBeaver's own bundled Java. The browser flow is verified as
far as Keycloak's sign-in page; the click-through itself is not something I can
test for you.*

### A5b. Switching between accounts on one machine

Sign in once through the browser and Keycloak keeps you signed in for **30
minutes idle / 8 hours max**. Every later connection from that browser is
answered from that session, with **no login prompt and a token for the first
account**. So after connecting as `admin`, a second connection is still `admin`:
silently, if the URL has no `user=`, or as `cannot impersonate user admin` if it
does.

Two things make this worse, so check both:

- **`externalAuthenticationTokenCache`** must not be `MEMORY`. Its default is
  `NONE`. `MEMORY` caches the token for the whole DBeaver session, so every
  connection reuses the first identity even across browsers.
- **The Keycloak session** persists in the browser regardless of DBeaver.

To switch account, do one of these:

1. **Sign out of Keycloak** first, then connect:
   <http://localhost:8180/realms/nda/protocol/openid-connect/logout>
2. **Force a prompt** by appending `&prompt=login` to the authorization request -
   Keycloak honours it and asks again even with a live session.
3. **Use token URLs for each account** (A1). Each URL carries its own identity,
   no browser and no shared session involved. This is the right choice when you
   are testing several personas rather than being one person.

One person with one account never hits this. It only appears when you are
checking what different roles see.

### A6. If you want to use Driver properties instead

They are on the **Driver properties** tab of the same dialog (next to **Main**;
in some builds it is in the left-hand tree under **Connection settings**). Set
`SSL` = `true` and `SSLUseSystemTrustStore` = `true`, plus either
`externalAuthentication` = `true` or `accessToken` = your token. This is the
same thing as the URL, just harder to see. If the tab is not there, use the URL.

## B. VS Code

### B1. One-time setup

1. Install the **Python** and **Jupyter** extensions (Extensions panel, search
   for each, Install).
2. Install the packages:

   ```bash
   pip install trino pandas ipykernel
   ```

### B2. Open the ready-made notebook

1. **File** → **Open Folder** → the `kpi-dashboard` folder.
2. Open `notebooks/marketplace_quickstart.ipynb`.
3. Top right → **Select Kernel** → **Python Environments** → your Python 3.11.
4. Click **Run All**.

The first cell finds the project, the second connects, and the rest list the
products, query one, and create a view in your sandbox.

### B3. Or connect from a new notebook

New file → `scratch.ipynb` → paste into a cell:

```python
import os, pandas as pd
from trino.dbapi import connect
from trino.auth import JWTAuthentication

CA = r"C:/Users/alber/OneDrive/Data Engineering/Projects/kpi-nodejs/kpi-dashboard-orig/kpi-dashboard/marketplace/generated/trino/tls/marketplace-ca.crt"

conn = connect(
    host="127.0.0.1", port=8443,
    catalog="iceberg", schema="marketplace",
    http_scheme="https", verify=CA,
    user="alice.nakato",
    auth=JWTAuthentication(os.environ["TRINO_JWT"]),
)

cur = conn.cursor()
cur.execute("SHOW TABLES")
print([r[0] for r in cur.fetchall()])
```

Before starting VS Code, put a token in the environment:

```bash
set -a && . ./streaming/.env && set +a
export TRINO_JWT=$(python -m marketplace.auth token alice.nakato)
code .
```

To sign in through the browser instead of using a token, swap the two auth lines:

```python
from trino.auth import OAuth2Authentication
# ...
    auth=OAuth2Authentication(),     # prints a sign-in URL in the cell output
```

Run the cell, scroll to the URL it prints, open it, sign in, then run again.

### B4. Query and get a DataFrame

```python
cur.execute("""
    SELECT process, workflow_stage, avg_waiting_days
    FROM delay_analysis
    WHERE process = 'GMP'
    ORDER BY avg_waiting_days DESC
    LIMIT 5
""")
pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])
```

```
process workflow_stage  avg_waiting_days
    GMP    CAPA Review               5.5
    GMP    CAPA Review               5.5
    GMP    CAPA Review               5.4
```

---

## C. Jupyter

### C1. Start it with a token in the environment

```bash
pip install trino pandas notebook

cd "<the kpi-dashboard folder>"
set -a && . ./streaming/.env && set +a
export TRINO_JWT=$(python -m marketplace.auth token alice.nakato)

jupyter notebook
```

Your browser opens the file list.

### C2. Open the notebook

Click into `notebooks/` → `marketplace_quickstart.ipynb` → **Cell** → **Run All**.

For a new notebook, use the same code as **B3**.

---

## If it does not work

Run this first — it separates "who does Trino think you are" from "what may that
account do", which is where most of these errors actually sit:

```bash
python -m marketplace.auth diagnose alice.nakato
```

| What you see | What to do |
|---|---|
| `No host specified: jdbc:trino://localhost127.0.0.1:8443/...` | The Host box has two values in it. Clear it, type `127.0.0.1` only |
| `Failed to connect to localhost/[0:0:0:0:0:0:0:1]:8443` | Host is `localhost`. Change it to `127.0.0.1` |
| `unexpected end of stream on http://127.0.0.1:8443` with `content=1503...` | `SSL=true` is missing, so the driver sent plain HTTP to a TLS port. Put the settings in the URL (A1-A2) |
| `TLS/SSL is required for authentication with username and password` | Clear the Password box, and check `SSL` is `true` |
| `Authentication failed: Unauthorized` | Your token expired. Get a new one: `python -m marketplace.auth token alice.nakato` |
| `PKIX path building failed` / `unable to find valid certification path` | Step 0 has not run on this machine |
| `CERTIFICATE_VERIFY_FAILED` (Python) | `CA=` is missing or the path is wrong. Use a full path with forward slashes |
| `cannot impersonate user ...` | Username does not match the token's account. Set it to `alice.nakato` |
| `Access Denied: Cannot access catalog iceberg` | You are signed in but in no group. Ask an administrator to add you to one |
| `Schema does not exist` for your sandbox | Ask an administrator to run `python -m marketplace.identity provision` |

Do **not** set `SSLVerification=NONE` or `verify=False` to get past a
certificate error. It connects, and it also hands your token to whoever answered.

## Other accounts

Swap the username. Each one sees a different slice:

| Username | Sees |
|---|---|
| `public.viewer` | 4 products, no physical tables |
| `alice.nakato` | 5 products + `nda_gold`, `entity_id` hashed |
| `sam.scientist` | 5 products + `nda_gold` + `nda_silver` |
| `dana.okello` | everything, read-write |

Passwords for these demo accounts are in `docs/ACCESS.md`.
