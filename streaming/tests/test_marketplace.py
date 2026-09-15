"""The governance model: policy attaches to groups, and the directory decides
who is in them. These tests guard the parts that fail silently if they break -
a rule accidentally written against a person, a role that exists in Terraform
but not in the marketplace, or a directory outage resolving to "open".
"""
import json
import os
import pathlib

import pytest

from marketplace import identity
from marketplace.build import access_rules
from marketplace.products import (MARKETPLACE_SCHEMA, PRODUCT_AUDIENCE, PRODUCTS,
                                  ROLES, SEED_MEMBERS, all_users, sandbox_schema)


# --------------------------------------------------------------------------
# Policy is group-shaped
# --------------------------------------------------------------------------
def test_every_production_grant_is_made_to_a_group_not_a_person():
    """The one permitted use of a username is the personal sandbox."""
    rules = access_rules()
    for section in ("catalogs", "schemas", "tables"):
        for rule in rules[section]:
            if "user" not in rule:
                continue
            schema = rule.get("schema", "")
            assert schema.startswith("sandbox_"), (
                f"{section} rule grants to the user {rule['user']!r} on {schema!r}; "
                "production access must be granted to a group")


def test_sandbox_rules_are_derived_from_membership_not_authored():
    """Pass a different cohort and the per-user rows follow it exactly."""
    cohort = ["ada.lovelace", "grace.hopper"]
    rules = access_rules(users=cohort)
    named = {rule["user"] for section in ("schemas", "tables")
             for rule in rules[section] if "user" in rule}
    assert named == set(cohort)
    schemas = {rule["schema"] for rule in rules["schemas"] if "user" in rule}
    assert schemas == {"sandbox_ada_lovelace", "sandbox_grace_hopper"}


def test_no_role_outranks_the_default_deny():
    rules = access_rules()
    assert rules["tables"][-1] == {"privileges": []}
    assert rules["schemas"][-1] == {"schema": ".*", "owner": False}


def test_restricted_product_is_not_reachable_by_business_users():
    audience = PRODUCT_AUDIENCE["quality_metrics"]
    assert "business_user" not in audience
    granting = [r for r in access_rules()["tables"]
                if r.get("table") == "quality_metrics"]
    assert granting and all("business_user" not in r["group"] for r in granting)


def test_entity_id_is_masked_on_the_rule_that_grants_select():
    """Trino applies the first matching table rule, so a mask on a later rule is
    never reached - it fails silently, showing raw values while looking set."""
    for rule in access_rules()["tables"]:
        if rule.get("group") in ("(analyst|data_scientist)", "data_scientist"):
            assert rule["privileges"] == ["SELECT"]
            assert [c["name"] for c in rule["columns"]] == ["entity_id"]
            assert "sha256" in rule["columns"][0]["mask"]


# --------------------------------------------------------------------------
# The directory is the source of membership
# --------------------------------------------------------------------------
def test_terraform_group_roles_are_roles_the_marketplace_knows():
    """A typo in trino_role means a group silently resolves to nothing."""
    source = pathlib.Path("infra/variables.tf").read_text(encoding="utf-8")
    declared = {line.split("=", 1)[1].strip().strip('"')
                for line in source.splitlines() if line.strip().startswith("trino_role")
                and "=" in line and "string" not in line}
    assert declared, "no trino_role attributes found in infra/variables.tf"
    assert declared <= set(ROLES), f"unknown role(s) in Terraform: {declared - set(ROLES)}"


def test_every_marketplace_role_has_a_group_that_grants_it():
    source = pathlib.Path("infra/variables.tf").read_text(encoding="utf-8")
    declared = {line.split("=", 1)[1].strip().strip('"')
                for line in source.splitlines() if line.strip().startswith("trino_role")
                and "=" in line and "string" not in line}
    assert set(ROLES) <= declared, f"no Keycloak group maps to {set(ROLES) - declared}"


def test_directory_failure_is_stale_not_open():
    """An unreachable Keycloak must raise, never resolve to a permissive default."""
    def explode(*_args, **_kwargs):
        raise identity.DirectoryUnavailable("simulated outage")

    original = identity.directory
    identity.directory = explode
    try:
        with pytest.raises(identity.DirectoryUnavailable):
            identity.role_members()
    finally:
        identity.directory = original


def test_empty_directory_is_treated_as_misconfigured_not_as_an_empty_org():
    original = identity.directory
    identity.directory = lambda *a, **k: []
    try:
        with pytest.raises(identity.DirectoryUnavailable):
            identity.role_members()
    finally:
        identity.directory = original


def test_disabled_accounts_lose_access_without_a_policy_edit():
    original = identity.directory
    identity.directory = lambda *a, **k: [{
        "name": "ma-analysts", "path": "/ma-analysts", "role": "analyst", "inherited": False,
        "members": [{"username": "alice.nakato", "enabled": True},
                    {"username": "leaver.person", "enabled": False}]}]
    try:
        members = identity.role_members()
        assert members["analyst"] == ["alice.nakato"]
    finally:
        identity.directory = original


def test_platform_identities_survive_a_directory_that_omits_them():
    """The view chain must not break because Keycloak forgot the view owners."""
    original = identity.directory
    identity.directory = lambda *a, **k: [{
        "name": "public", "path": "/public", "role": "business_user", "inherited": False,
        "members": [{"username": "public.viewer", "enabled": True}]}]
    try:
        members = identity.role_members()
        assert set(members["administrator"]) >= {"marketplace_owner", "nda_dashboard"}
    finally:
        identity.directory = original


def test_a_group_carrying_an_unknown_role_is_an_error_not_a_silent_denial():
    original = identity.directory
    identity.directory = lambda *a, **k: [{
        "name": "typo-group", "path": "/typo-group", "role": "analysts", "inherited": False,
        "members": [{"username": "someone", "enabled": True}]}]
    try:
        with pytest.raises(identity.DirectoryUnavailable, match="not a known role"):
            identity.role_members()
    finally:
        identity.directory = original


def test_group_file_is_the_format_trinos_file_provider_reads():
    rendered = identity.group_file({"analyst": ["alice.nakato", "grace.auma"],
                                    "administrator": ["admin"]})
    assert rendered.splitlines() == ["administrator:admin", "analyst:alice.nakato,grace.auma"]


def test_offline_bootstrap_uses_the_seed_and_says_so():
    members = identity.role_members(offline=True)
    assert set(members) == set(SEED_MEMBERS)
    assert set(members["analyst"]) == set(SEED_MEMBERS["analyst"])


def test_sandboxes_are_derived_from_whoever_holds_a_role():
    members = {"analyst": ["alice.nakato"], "administrator": ["marketplace_owner"]}
    assert identity.governed_users(members) == ["alice.nakato", "marketplace_owner"]


def test_unsafe_usernames_cannot_reach_a_schema_name():
    for hostile in ("bob; DROP SCHEMA", "../etc", "Robert'); --", ""):
        with pytest.raises(ValueError):
            sandbox_schema(hostile)
    assert identity.governed_users({"analyst": ["bad name", "alice.nakato"]}) == ["alice.nakato"]


# --------------------------------------------------------------------------
# The two sides agree
# --------------------------------------------------------------------------
def test_seed_cohort_only_names_roles_that_exist():
    assert set(SEED_MEMBERS) <= set(ROLES)


def test_opa_document_carries_the_same_role_mapping():
    generated = pathlib.Path("infra/generated/data.json")
    if not generated.exists():
        pytest.skip("terraform apply has not run")
    entitlements = json.loads(generated.read_text(encoding="utf-8"))["entitlements"]
    for name, rights in entitlements.items():
        assert rights.get("trino_role") in ROLES, f"{name}: {rights.get('trino_role')!r}"


def test_every_product_is_reachable_by_at_least_one_role():
    for product in PRODUCTS:
        assert PRODUCT_AUDIENCE.get(product.name), (
            f"{product.name} has no audience, so it is published but invisible")
        assert product.fqn.startswith(f"iceberg.{MARKETPLACE_SCHEMA}.")


def test_seed_users_all_produce_distinct_sandboxes():
    schemas = [sandbox_schema(u) for u in all_users()]
    assert len(set(schemas)) == len(schemas)


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
def test_tls_certificate_covers_every_name_the_coordinator_answers_to():
    from marketplace import tls
    if not tls.KEYSTORE.exists():
        pytest.skip("no keystore issued yet")
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import pkcs12
    _, server, _ = pkcs12.load_key_and_certificates(
        tls.KEYSTORE.read_bytes(), tls.keystore_password().encode())
    alternatives = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    names = set(alternatives.get_values_for_type(x509.DNSName))
    addresses = {str(a) for a in alternatives.get_values_for_type(x509.IPAddress)}
    assert set(tls.HOSTNAMES) <= names
    assert set(tls.ADDRESSES) <= addresses


def test_keystore_and_internal_secret_are_distinct():
    """Reusing one secret across two trust boundaries makes rotation break things."""
    from marketplace import tls
    if not tls.KEYSTORE.exists():
        pytest.skip("no keystore issued yet")
    assert tls.keystore_password() != tls.internal_secret()


def test_authentication_properties_pin_the_principal_claim():
    from marketplace import trino_config
    from marketplace.auth import required
    if not required():
        properties = trino_config.authentication_properties()
        assert properties == [], "no OIDC client yet, so the coordinator must stay plain"
        return
    properties = trino_config.authentication_properties()
    joined = chr(10).join(properties)
    assert "http-server.authentication.type=OAUTH2,JWT" in joined
    # Both flows must derive the principal the same way, or the two routes
    # disagree about who you are.
    assert joined.count("principal-field=preferred_username") == 2
    assert "http-server.https.enabled=true" in joined
    assert "required-audience=" in joined
    assert "required-issuer=" in joined


def test_impersonation_is_denied_by_default():
    """Forgetting to grant it must deny, not permit."""
    rules = access_rules()["impersonation"]
    assert all(rule["allow"] is False for rule in rules)
    assert rules[-1] == {"original_user": ".*", "new_user": ".*", "allow": False}


def test_only_named_administrators_may_impersonate():
    rules = access_rules(impersonators=["admin", "dana.okello"])["impersonation"]
    allowed = [r["original_user"] for r in rules if r["allow"]]
    assert allowed == ["admin", "dana.okello"]
    # ...and the catch-all deny still sits last, so nobody else slips through.
    assert rules[-1]["allow"] is False


def test_nobody_may_impersonate_a_platform_principal():
    """Becoming a view owner would hand over the privileges every certified view
    runs with, so the denial is the FIRST rule - Trino uses first match."""
    from marketplace.build import PLATFORM_PRINCIPALS
    rules = access_rules(impersonators=["admin"])["impersonation"]
    assert rules[0]["allow"] is False
    for principal in PLATFORM_PRINCIPALS:
        assert principal in rules[0]["new_user"]


def test_impersonation_is_off_unless_explicitly_enabled(monkeypatch):
    from marketplace import trino_config
    members = {"administrator": ["admin"], "analyst": ["alice.nakato"]}
    monkeypatch.delenv("MARKETPLACE_ALLOW_IMPERSONATION", raising=False)
    assert trino_config.impersonators(members) == ()
    monkeypatch.setenv("MARKETPLACE_ALLOW_IMPERSONATION", "1")
    assert trino_config.impersonators(members) == ("admin",)


def test_audit_records_the_real_principal_not_just_the_effective_user():
    """Under impersonation these differ, and that is exactly when it matters:
    without the principal, a support session reads as the person being viewed."""
    from marketplace.audit import COLUMNS, flatten
    assert "principal" in COLUMNS
    row = flatten({"metadata": {"queryId": "q1", "query": "SELECT 1"},
                   "context": {"user": "alice.nakato", "principal": "admin"},
                   "endTime": "2026-09-15T10:00:00Z"})
    assert row["username"] == "alice.nakato"
    assert row["principal"] == "admin"


def test_secrets_are_not_committed():
    import subprocess
    for path in ("infra/generated/trino_oidc.json",
                 "infra/generated/credentials.json",
                 "marketplace/generated/trino/tls/marketplace-ca.key",
                 "marketplace/generated/trino/tls/trino.p12"):
        ignored = subprocess.run(["git", "check-ignore", "-q", path]).returncode == 0
        assert ignored, f"{path} is not gitignored"


def test_truststore_holds_a_certificate_java_will_actually_trust():
    """A PKCS12 written by a generic library loads fine and trusts nothing."""
    import subprocess
    from marketplace import tls
    if not tls.TRUSTSTORE.exists():
        pytest.skip("no trust store issued yet")
    mount = str(tls.TLS.resolve()).replace(chr(92), "/")
    listing = subprocess.run([
        "docker", "run", "--rm", "-v", f"{mount}:/tls", "--entrypoint", "keytool",
        os.environ.get("TRINO_IMAGE", "trinodb/trino:455"),
        "-list", "-keystore", f"/tls/{tls.TRUSTSTORE.name}",
        "-storetype", "PKCS12", "-storepass", tls.keystore_password()],
        capture_output=True, text=True, env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    if listing.returncode != 0:
        pytest.skip("docker not available to run keytool")
    assert "trustedCertEntry" in listing.stdout, listing.stdout[:300]


def test_ports_are_published_on_both_loopback_families():
    """localhost resolves to ::1 first; binding only IPv4 breaks every JVM client."""
    from marketplace.trino_config import loopback
    published = loopback(8443, 8443)
    assert published.count("-p") == 2
    assert "127.0.0.1:8443:8443" in published
    assert "[::1]:8443:8443" in published
    # Loopback only - never a routable address.
    assert not any(spec.startswith("0.0.0.0") for spec in published)


def test_browser_connection_requires_a_real_session_user(monkeypatch):
    from marketplace import auth
    monkeypatch.delenv("TRINO_USER", raising=False)
    with pytest.raises(auth.NotAuthenticated, match="Keycloak username"):
        auth.browser_connect()


def test_dbeaver_defaults_to_browser_login_without_issuing_a_token(monkeypatch, capsys):
    from marketplace import auth
    def forbidden(*args, **kwargs):
        pytest.fail("Browser setup must not read credentials or mint bearer tokens")
    monkeypatch.setattr(auth, "access_token", forbidden)
    monkeypatch.setattr(auth, "password_for", forbidden)
    auth.dbeaver("alice.nakato", url_form=True)
    output = capsys.readouterr().out
    assert "externalAuthentication=true" in output
    assert "SSLUseSystemTrustStore=true" in output
    assert "user=alice.nakato" in output
    assert "accessToken=" not in output


def test_sso_url_has_one_unambiguous_identity():
    from urllib.parse import parse_qs, urlsplit
    from marketplace.auth import sso_jdbc_url
    url = sso_jdbc_url("alice+test@example.org")
    params = parse_qs(urlsplit(url.removeprefix("jdbc:")).query)
    assert params["user"] == ["alice+test@example.org"]
    assert params["externalAuthenticationTokenCache"] == ["NONE"]
    assert "password" not in params and "accessToken" not in params


def test_browser_connection_uses_explicit_or_environment_identity(monkeypatch):
    import trino
    from marketplace import auth
    monkeypatch.setenv("TRINO_USER", "alice.nakato")
    monkeypatch.setattr(auth, "ca_bundle", lambda: "test-ca.crt")
    monkeypatch.setattr(trino.dbapi, "connect", lambda **kwargs: kwargs)
    assert auth.browser_connect()["user"] == "alice.nakato"
    assert auth.browser_connect("dana.okello")["user"] == "dana.okello"


def test_rendered_config_uses_linux_line_endings(monkeypatch, tmp_path):
    from marketplace import trino_config
    monkeypatch.setattr(trino_config, "OUT", tmp_path)
    monkeypatch.setattr(trino_config, "config_files", lambda **kwargs: {
        "config.properties": "http-server.http.port=8080\ncoordinator=true\n"})
    root = trino_config.write()
    assert (root / "config.properties").read_bytes() == (
        b"http-server.http.port=8080\ncoordinator=true\n")


def test_jdbc_url_encodes_paths_that_would_otherwise_break_it():
    """A Windows path breaks a JDBC URL twice: the drive-letter colon truncates
    the value, and a space in a folder name ends it. The driver then rejects the
    whole URL as invalid without saying which parameter was at fault."""
    from marketplace import auth
    if not auth.required():
        pytest.skip("no OIDC client, so no token to build a URL with")
    try:
        url = auth.jdbc_url("alice.nakato")
    except auth.NotAuthenticated as error:
        # The URL needs a live token. A stopped cluster is not a test failure.
        pytest.skip(f"cluster unreachable: {str(error).splitlines()[0][:60]}")
    query = url.split("?", 1)[1]
    assert " " not in query, "an unencoded space makes the URL invalid"
    # Only the scheme and host:port colons may remain unencoded.
    assert ":" not in query, "an unencoded colon truncates the parameter"
    assert chr(92) not in query, "backslashes are not valid in a JDBC URL"
    assert "SSL=true" in query, "without SSL the driver refuses before connecting"
    assert "password=" not in query.lower() or "SSLTrustStorePassword" in query, (
        "a user password must never appear: this cluster has no password authenticator")
