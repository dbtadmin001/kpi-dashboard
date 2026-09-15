"""Getting a proven identity to Trino.

Before this, `user="alice.nakato"` was a string a client asserted. Trino believed
it, which meant `user="admin"` also worked, and DBeaver quietly sent the operating
system account - producing "Access Denied" for a principal nobody had ever heard
of. The rules were sound and the identity behind them was not.

Now the coordinator authenticates. Every connection carries a token from
Keycloak, and Trino takes the principal from the token's `preferred_username`
claim, so the username is something Keycloak asserted rather than something the
caller typed.

Two ways in, matching the two kinds of caller:

*   **A person** uses the browser flow - `trino.auth.OAuth2Authentication`, the
    Trino CLI's `--external-authentication`, or DBeaver. No password reaches the
    tool; Trino prints a URL, Keycloak does the signing in, and the token is
    cached by the client.

*   **A program** - dbt, a notebook, the platform's own services - exchanges a
    username and password for a JWT here. Tokens are cached in memory until
    shortly before they expire, so a long dbt run does not re-authenticate on
    every statement.

    from marketplace.auth import connect
    with connect("alice.nakato") as conn:      # password from the environment
        ...

Service accounts read their password from the Terraform-generated credentials
file, which is gitignored; humans supply theirs, or use the browser flow.
"""
import json
import os
import pathlib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
OIDC_FILE = pathlib.Path(os.environ.get("MARKETPLACE_OIDC_FILE", ROOT / "infra/generated/trino_oidc.json"))
CREDENTIALS_FILE = pathlib.Path(os.environ.get("MARKETPLACE_CREDENTIALS_FILE", ROOT / "infra/generated/credentials.json"))
CA_FILE = pathlib.Path(os.environ.get("MARKETPLACE_CA_FILE", ROOT / "marketplace/generated/trino/tls/marketplace-ca.crt"))

HOST = os.environ.get("MARKETPLACE_TRINO_HOST", "localhost")
PORT = int(os.environ.get("MARKETPLACE_TRINO_HTTPS_PORT", "8443"))

# Refresh this long before expiry rather than at it, so a statement issued just
# under the wire does not arrive just over it.
EXPIRY_MARGIN = 60
_tokens = {}


class NotAuthenticated(RuntimeError):
    """No usable credential. Never silently downgraded to an unauthenticated call."""


def oidc():
    """Client id, secret and endpoints, written by Terraform."""
    if not OIDC_FILE.exists():
        raise NotAuthenticated(
            f"{OIDC_FILE} does not exist - the Trino OIDC client has not been created."
            f"{chr(10)}  ./infra/run.sh apply")
    return json.loads(OIDC_FILE.read_text(encoding="utf-8"))


def _token_url():
    """Prefer the address this process can actually reach."""
    settings = oidc()
    inside = os.path.exists("/.dockerenv")
    return settings["internal_token_url"] if inside else settings["token_url"]


def password_for(username):
    """A seed or service account's password, from the generated credentials file.

    Environment first, so a real deployment can inject secrets without a file:
    TRINO_PASSWORD for the current user, or MARKETPLACE_PASSWORD_<USER>.
    """
    specific = os.environ.get("MARKETPLACE_PASSWORD_" + username.upper().replace(".", "_"))
    if specific:
        return specific
    if os.environ.get("TRINO_USER") == username and os.environ.get("TRINO_PASSWORD"):
        return os.environ["TRINO_PASSWORD"]
    if CREDENTIALS_FILE.exists():
        users = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8")).get("users", {})
        if username in users:
            return users[username]["password"]
    raise NotAuthenticated(
        f"No password for {username!r}."
        f"{chr(10)}  Service and seed accounts: run ./infra/run.sh apply to generate"
        f"{chr(10)}  {CREDENTIALS_FILE}"
        f"{chr(10)}  Anyone else: export MARKETPLACE_PASSWORD_"
        + username.upper().replace(".", "_") + "=...")


def access_token(username, password=None):
    """Exchange a username and password for a JWT. Cached until nearly expired."""
    cached = _tokens.get(username)
    if cached and cached["expires_at"] > time.time() + EXPIRY_MARGIN:
        return cached["token"]
    settings = oidc()
    body = urllib.parse.urlencode({
        "grant_type": "password",
        "client_id": settings["client_id"],
        "client_secret": settings["client_secret"],
        "username": username,
        "password": password if password is not None else password_for(username),
        "scope": "openid",
    }).encode()
    try:
        with urllib.request.urlopen(_token_url(), body, timeout=20) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        raise NotAuthenticated(
            f"Keycloak refused to authenticate {username!r}: {error.code} {detail}") from None
    except (urllib.error.URLError, OSError) as error:
        raise NotAuthenticated(f"Cannot reach Keycloak at {_token_url()}: {error}") from None
    _tokens[username] = {"token": payload["access_token"],
                         "expires_at": time.time() + payload.get("expires_in", 300)}
    return payload["access_token"]


def ca_bundle():
    """The path clients pass as `verify`, so the dev CA is trusted by name.

    Returning the CA rather than False matters: `verify=False` would also accept
    a certificate from anybody else, which defeats the point of encrypting the
    token in the first place.
    """
    if not CA_FILE.exists():
        raise NotAuthenticated(
            f"No CA certificate at {CA_FILE}."
            f"{chr(10)}  python -m marketplace.tls issue")
    return str(CA_FILE)


def connect(user, password=None, catalog="iceberg", schema=None, **extra):
    """An authenticated Trino connection. The username comes from the token."""
    import trino

    return trino.dbapi.connect(
        host=extra.pop("host", HOST), port=extra.pop("port", PORT),
        user=user, catalog=catalog, schema=schema,
        http_scheme="https", verify=ca_bundle(),
        auth=trino.auth.JWTAuthentication(access_token(user, password)),
        request_timeout=extra.pop("request_timeout", 120), **extra)


def browser_connect(user=None, catalog="iceberg", schema=None, **extra):
    """The flow a person should use: Trino prints a URL, Keycloak does the rest.

    No password is handled by the client at all. Useful from a notebook when you
    would rather not put your own password in a cell. Supply your Keycloak
    username (or TRINO_USER) and sign in as that same user in the browser.
    """
    import trino

    user = user or os.environ.get("TRINO_USER")
    if not user or not user.strip():
        raise NotAuthenticated(
            "Supply your Keycloak username: browser_connect('alice.nakato'), "
            "or set TRINO_USER. Sign in as that same user in the browser.")
    return trino.dbapi.connect(
        host=extra.pop("host", HOST), port=extra.pop("port", PORT),
        user=user, catalog=catalog, schema=schema,
        http_scheme="https", verify=ca_bundle(),
        auth=trino.auth.OAuth2Authentication(),
        request_timeout=extra.pop("request_timeout", 120), **extra)


def ssl_context():
    """For callers that speak HTTP directly rather than through the Trino client."""
    context = ssl.create_default_context(cafile=ca_bundle())
    return context


def required():
    """True once an OIDC client exists, i.e. once the coordinator demands a token."""
    if os.environ.get("MARKETPLACE_REQUIRE_AUTH") == "1" and not OIDC_FILE.exists():
        raise NotAuthenticated("Authentication is mandatory but the OIDC configuration is missing")
    return OIDC_FILE.exists()


def connect_any(user, catalog="iceberg", schema=None, **extra):
    """Authenticated when the cluster requires it, plain HTTP while it does not.

    Every service in the platform connects through this, so enabling
    authentication is one Terraform apply rather than an edit in six modules -
    and so no module can be left behind still using the unauthenticated port.
    """
    if required():
        return connect(user, catalog=catalog, schema=schema, **extra)
    import trino
    return trino.dbapi.connect(
        host=extra.pop("host", os.environ.get("TRINO_HOST", "127.0.0.1")),
        port=extra.pop("port", int(os.environ.get("TRINO_PORT", "8090"))),
        user=user, catalog=catalog, schema=schema,
        http_scheme=os.environ.get("TRINO_SCHEME", "http"),
        request_timeout=extra.pop("request_timeout", 120), **extra)


def whoami(user, password=None):
    """What Trino believes about this principal. The first thing to run when a
    client reports 'Access Denied' - it separates 'who are you' from 'may you'."""
    conn = connect_any(user) if not required() else connect(user, password)
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT current_user, current_groups()")
            principal, groups = cursor.fetchone()
        finally:
            cursor.close()
    finally:
        conn.close()
    return {"principal": principal, "groups": list(groups or [])}


def diagnose(user):
    """Answer 'why was I denied?' in the order the answers actually matter.

    Almost every report of "access denied on everything" turns out to be an
    identity problem rather than a policy one - most often a tool that quietly
    sent the operating system account. This says so in one line instead of
    leaving someone to read access rules that were never the problem.
    """
    print(f"Diagnosing {user!r} against "
          f"{'https' if required() else 'http'}://{HOST}:{PORT if required() else 8090}")
    print()
    if not required():
        print("  [!] The coordinator is NOT authenticating. Any username is accepted")
        print("      as typed, so 'access denied' here means the username is simply")
        print("      not in any group - check spelling, then Keycloak membership.")
    else:
        print("  [ok] The coordinator requires a token; the username is proven.")
    try:
        identity = whoami(user)
    except Exception as error:                      # noqa: BLE001 - this IS the report
        first = str(error).splitlines()[0]
        print(f"  [!] Could not connect as {user}: {first}")
        if "401" in first or "refused to authenticate" in first:
            print("      Keycloak rejected the credential. Wrong password, or the")
            print("      account is disabled in the realm.")
        elif "CERTIFICATE_VERIFY_FAILED" in first:
            print(f"      The client does not trust the dev CA. Point it at {CA_FILE}")
        return 1
    print(f"  principal  {identity['principal']}")
    print(f"  groups     {', '.join(identity['groups']) or '(none)'}")
    if not identity["groups"]:
        print()
        print("  [!] No groups. This is the whole problem: the access rules grant to")
        print("      groups, so a principal in none of them matches only the default")
        print("      deny and every schema is invisible.")
        print("      Fix: add the account to a group in Keycloak, then")
        print("           python -m marketplace.identity sync")
        return 1
    conn = connect_any(user)
    try:
        cursor = conn.cursor()
        cursor.execute("SHOW SCHEMAS FROM iceberg")
        schemas = [r[0] for r in cursor.fetchall() if r[0] != "information_schema"]
        cursor.close()
    finally:
        conn.close()
    print(f"  can see    {', '.join(schemas) or '(nothing)'}")
    return 0


def token_hours(token):
    """Hours of life left in a token, read from the token itself.

    Reported rather than assumed: the client's configured lifespan is capped by
    the realm's SSO session limit, so the number in Terraform is an upper bound,
    not the answer.
    """
    import base64
    body = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    return max(0.0, (claims["exp"] - time.time()) / 3600)


def jdbc_url(user, schema="marketplace"):
    """The whole connection as one string, for DBeaver's "Connect by URL" mode.

    Every field in DBeaver's form is a chance to get this wrong, and two of the
    mistakes produce errors that point somewhere else:

      * a value left in the Password box  -> "TLS/SSL is required for
        authentication with username and password", raised by the driver before
        it contacts the server, so the server logs show nothing;
      * SSL not set to true               -> the same message, same reason.

    Username and password are BOTH wrong here in any case: this coordinator runs
    OAUTH2 and JWT authenticators only, so a password fails with "Authentication
    failed: Unauthorized" even over TLS. The credential is the token.
    """
    from .tls import TRUSTSTORE, keystore_password

    parameters = {
        "user": user,
        "SSL": "true",
        # Forward slashes: Java accepts them on Windows, and they need no
        # escaping beyond what percent-encoding already does.
        "SSLTrustStorePath": str(TRUSTSTORE).replace(chr(92), "/"),
        "SSLTrustStorePassword": keystore_password(),
        "SSLTrustStoreType": "PKCS12",
        "accessToken": access_token(user),
    }
    # Percent-encode every value. A Windows path breaks the URL twice over
    # otherwise: the drive-letter colon truncates the value and the driver
    # rejects the whole thing as "Invalid JDBC URL", and a space in a folder
    # name does the same. Both are silent about which parameter was at fault.
    query = "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in parameters.items())
    return f"jdbc:trino://127.0.0.1:{PORT}/iceberg/{schema}?{query}"


def sso_jdbc_url(user, schema="marketplace"):
    """A reusable browser-login URL containing no password or access token.

    Trust the marketplace CA in Windows first. An explicit user also prevents
    DBeaver's OS username becoming the requested Trino session identity.
    """
    if not user or not user.strip():
        raise NotAuthenticated("Supply your Keycloak username for DBeaver SSO.")
    parameters = {
        "user": user, "SSL": "true", "SSLUseSystemTrustStore": "true",
        "externalAuthentication": "true",
        # Do not share a cached login across different users' test connections.
        "externalAuthenticationTokenCache": "NONE",
    }
    query = urllib.parse.urlencode(parameters, quote_via=urllib.parse.quote)
    return f"jdbc:trino://127.0.0.1:{PORT}/iceberg/{schema}?{query}"


def dbeaver(user, url_form=False, token_form=False):
    """Print browser SSO settings; token-based setup requires explicit opt-in."""
    if not token_form:
        print("DBeaver -> New Connection -> Trino")
        if url_form:
            print("Connect by: URL. Paste this URL:")
            print(sso_jdbc_url(user))
            print("Leave the separate Username and Password fields empty; user is in the URL.")
            print("Do not duplicate URL parameters in Driver properties.")
        else:
            print(f"  Host: 127.0.0.1   Port: {PORT}   Database/Schema: iceberg/marketplace")
            print(f"  Username: {user}   Password: leave empty (disable Save password)")
            print("  Driver properties:")
            print("    SSL=true")
            print("    SSLUseSystemTrustStore=true")
            print("    externalAuthentication=true")
            print("    externalAuthenticationTokenCache=NONE")
        print("Remove any old accessToken, password, and sessionUser driver properties.")
        print("Trust the marketplace CA once: python -m marketplace.tls trust --apply")
        print(f"Test Connection opens Keycloak. Sign in as {user}.")
        print("If Keycloak signs in as another account, log out there and reconnect.")
        return
    _dbeaver_token(user, url_form)


def _dbeaver_token(user, url_form=False):
    """Connection settings for a tool that cannot do single sign-on.

    THIS IS NOT THE NORMAL PATH. Normally a person connects with

        SSL=true  SSLUseSystemTrustStore=true  externalAuthentication=true

    and signs in through the browser as themselves - identical settings for
    everyone, no token handling, nothing issued per user. That needs the CA in
    the machine's trust store: `python -m marketplace.tls trust --apply`, or the
    ca_trust tasks in Ansible for a managed fleet.

    What this prints is the fallback for a machine where the CA cannot be
    installed. It hands over a bearer token, which expires, and which somebody
    has to reissue - so it does not scale past a handful of people and should not
    be the documented workflow.
    """
    if url_form:
        print("DBeaver -> New Connection -> Trino -> Connect by: URL")
        print(f"{chr(10)}  Leave Username and Password EMPTY. Paste this as the URL:{chr(10)}")
        print(jdbc_url(user))
        print(f"{chr(10)}  Valid for {token_hours(access_token(user)):.1f} more hours.")
        return
    """Print a complete, filled-in DBeaver connection. Nothing left to assemble.

    The Username field is NOT optional and must equal the token's principal.
    Trino takes the session user from that field (or, if blank, from the client's
    OS account) and then refuses to let a token for one principal open a session
    as another - "cannot impersonate user". Leaving it empty is the single most
    common way this fails.
    """
    from .tls import TRUSTSTORE, keystore_password

    token = access_token(user)
    print(f"DBeaver -> New Connection -> Trino{chr(10)}")
    print("  Main tab")
    print(f"    Host          127.0.0.1          <- not 'localhost': Java tries ::1 first")
    print(f"    Port          {PORT}")
    print(f"    Database      iceberg")
    print(f"    Username      {user}             <- REQUIRED, and must match the token")
    print(f"    Password      (leave empty)")
    print(f"{chr(10)}  Driver properties tab")
    print(f"    SSL                       true")
    print(f"    SSLTrustStorePath         {TRUSTSTORE}")
    print(f"    SSLTrustStorePassword     {keystore_password()}")
    print(f"    SSLTrustStoreType         PKCS12")
    print(f"    accessToken               {token}")
    print(f"{chr(10)}  Valid for {token_hours(token):.1f} more hours. Re-run this for a fresh one.")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Authenticate to the marketplace coordinator")
    parser.add_argument("action", choices=["token", "whoami", "diagnose", "env", "dbeaver", "browser"])
    parser.add_argument("user", nargs="?", default=os.environ.get("TRINO_USER", "marketplace_owner"))
    parser.add_argument("--url", action="store_true",
                        help="Print one JDBC URL instead of a list of form fields")
    parser.add_argument("--token", action="store_true",
                        help="Explicitly use the expiring token fallback for DBeaver")
    args = parser.parse_args()
    try:
        if args.action == "token":
            print(access_token(args.user))
        elif args.action == "dbeaver":
            dbeaver(args.user, args.url, args.token)
        elif args.action == "browser":
            with browser_connect(args.user, schema="marketplace") as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute("SELECT current_user, current_groups()")
                    print("Identity:", cursor.fetchone())
                    cursor.execute("SHOW TABLES FROM iceberg.marketplace")
                    print("Visible datasets:", ", ".join(row[0] for row in cursor.fetchall()))
                finally:
                    cursor.close()
        elif args.action == "whoami":
            identity = whoami(args.user)
            print(f"  principal  {identity['principal']}")
            print(f"  groups     {', '.join(identity['groups']) or '(none)'}")
        elif args.action == "env":
            # Shell-eval'able, so dbt and notebooks can pick the token up without
            # it ever being typed into a file. Every value is quoted: the CA path
            # on Windows contains spaces and backslashes, and an unquoted one
            # makes `eval` fail with a baffling "not a valid identifier".
            import shlex
            for name, value in (("TRINO_JWT", access_token(args.user)),
                                ("TRINO_USER", args.user),
                                ("TRINO_CA", str(ca_bundle()).replace(chr(92), "/"))):
                print(f"export {name}={shlex.quote(value)}")
        else:
            raise SystemExit(diagnose(args.user))
    except NotAuthenticated as error:
        raise SystemExit(str(error))


if __name__ == "__main__":
    main()
