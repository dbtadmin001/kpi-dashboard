# Connect through Keycloak

The governed coordinator is `https://127.0.0.1:8443`. Its catalog/schema is
`iceberg/marketplace`. Port 8083 belongs to the separate pipeline coordinator.
These loopback addresses work on the Docker host only.

## DBeaver

The marketplace CA is already installed in this Windows user's trusted root
store. On another Windows client, install the CA once using
`python -m marketplace.tls trust --apply` after verifying it is your platform CA.

Create a Trino connection using **Host** mode:

| Setting | Value |
| --- | --- |
| Host | `127.0.0.1` |
| Port | `8443` |
| Database/Schema | `iceberg/marketplace` |
| Username | Your exact Keycloak username, e.g. `alice.nakato` |
| Password | Empty; uncheck Save password |
| Driver property `SSL` | `true` |
| Driver property `SSLUseSystemTrustStore` | `true` |
| Driver property `externalAuthentication` | `true` |
| Driver property `externalAuthenticationTokenCache` | `NONE` |

Remove stale `accessToken`, `password`, and `sessionUser` driver properties.
Use **Test Connection**, then sign in to Keycloak as the username above. The
driver receives the token through Trino's callback. You do not paste tokens or
enter your Keycloak password in DBeaver.

Alternatively, generate a reusable SSO URL:

```powershell
python -m marketplace.auth dbeaver alice.nakato --url
```

Use **URL** mode with that result. The username is already in the URL, so leave
the separate username/password boxes empty and do not duplicate URL properties
in Driver properties. Token setup is available only with the explicit `--token`
fallback; it expires and does not provide browser SSO.

When testing multiple users, Keycloak can reuse an existing browser login.
Log out of Keycloak before switching identities. A token for Alice cannot open
a session as Dana. `externalAuthenticationTokenCache=NONE` avoids sharing the
JDBC token cache, but does not log out the browser. Restart DBeaver if a previous
connection still holds the wrong user's token.

## VS Code

Open `notebooks/marketplace_quickstart.ipynb` with the Python and Jupyter
extensions. Select an environment with `trino`, `pandas`, and `ipykernel`
installed. The notebook now uses browser SSO and the local CA certificate.

You can also use the VS Code terminal from the repository root:

```powershell
python -m marketplace.auth browser alice.nakato
```

The first query opens Keycloak and prints the authenticated identity, roles,
and visible datasets. Sign in as the specified username. This path does not
read the generated seed credentials or request a password-grant token.

For your own Python code:

```python
from marketplace.auth import browser_connect

with browser_connect("alice.nakato", schema="marketplace") as conn:
    cur = conn.cursor()
    cur.execute("SELECT current_user, current_groups()")
    print(cur.fetchall())
    cur.close()
```

This is a Python/Jupyter integration; it does not assume every VS Code SQL
extension supports Trino's external-authentication protocol.

## Verify access and locate failures

Run in the authenticated client:

```sql
SELECT current_user, current_groups();
SHOW TABLES FROM iceberg.marketplace;
SELECT COUNT(*) FROM iceberg.marketplace.application_throughput;
```

| Error | Check |
| --- | --- |
| TLS/SSL required | Set `SSL=true`, clear the password, use port 8443. |
| PKIX / certificate validation failure | Trust the correct CA and enable `SSLUseSystemTrustStore`. Keep certificate validation enabled. |
| Connection refused | Verify the coordinator is running and the client is on the Docker host. |
| Cannot impersonate user | DBeaver username and Keycloak browser login differ. Reconnect as the same user. |
| Unauthorized without browser login | Enable `externalAuthentication`, remove old token/password properties. |
| Login succeeds, groups empty | Verify directory membership and its synchronization to Trino. |
| Specific table denied | Compare the user's role with that dataset's policy; a successful login does not grant every dataset. |

Trino currently resolves groups from a generated file, rather than directly
from the token's groups claim. After directory changes, the operator runs
`python -m marketplace.identity sync`, or keeps `python -m marketplace.identity watch`
running. Trino reloads the files within 30 seconds after synchronization.

The SSO settings follow the [Trino JDBC documentation](https://trino.io/docs/current/client/jdbc.html)
and [OAuth2 flow](https://trino.io/docs/current/security/oauth2.html).
