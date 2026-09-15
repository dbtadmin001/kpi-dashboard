# The Trino coordinator as an OIDC client.
#
# Until this existed, Trino accepted whatever username a client sent. DBeaver
# sends your OS account by default, so connecting produced "Access Denied" for a
# user nobody had ever heard of - and worse, typing `--user admin` would have
# worked. The username was a claim, not a fact.
#
# Two flows, one client, because the two kinds of caller differ:
#
#   standard flow (browser)  - DBeaver, the Trino CLI, anything a person drives.
#                              Trino redirects to Keycloak, the person signs in,
#                              Trino caches the token. No password in the tool.
#   direct access grant      - notebooks, dbt, the platform's own services. They
#                              exchange username+password for a JWT and send it
#                              as a bearer token.
#
# Both produce a token whose `preferred_username` claim becomes the Trino
# principal, which is then resolved to a role through the group file. Identity,
# authentication and authorisation finally meet in one place.
resource "keycloak_openid_client" "trino" {
  realm_id  = keycloak_realm.nda.id
  client_id = "trino"
  name      = "Trino marketplace coordinator"
  enabled   = true

  access_type                  = "CONFIDENTIAL"
  standard_flow_enabled        = true
  direct_access_grants_enabled = true
  service_accounts_enabled     = false

  # The realm default is 10 minutes, which is right for the dashboard: it
  # re-authorises constantly, so a regrouped user loses access in minutes. A
  # desktop SQL client is the opposite - it holds one connection all day, and a
  # 10-minute token means pasting a new one every 10 minutes, which is how people
  # end up disabling security to get work done. One working day, and revocation
  # still happens at the next token request.
  access_token_lifespan = var.trino_token_lifespan_seconds

  # Trino's OAuth2 callback. Both spellings of loopback, because whether the
  # browser lands on localhost or 127.0.0.1 depends on the client tool.
  valid_redirect_uris = [
    "https://localhost:${var.trino_https_port}/oauth2/callback",
    "https://127.0.0.1:${var.trino_https_port}/oauth2/callback",
    "https://localhost:${var.trino_https_port}/ui/*",
    "https://127.0.0.1:${var.trino_https_port}/ui/*",
  ]
  web_origins = ["+"]
}

# Trino checks the audience, so the token has to name Trino. Without this the
# token is valid but "not for you", which is the correct thing for Trino to
# reject and an unhelpful thing to debug.
resource "keycloak_openid_audience_protocol_mapper" "trino" {
  realm_id                 = keycloak_realm.nda.id
  client_id                = keycloak_openid_client.trino.id
  name                     = "trino-audience"
  included_client_audience = keycloak_openid_client.trino.client_id
  add_to_access_token      = true
  add_to_id_token          = false
}

# Group membership in the token. Trino resolves groups through its own group
# provider, so this is not what grants access - it is here so a token can be
# inspected and understood without a second call to Keycloak.
resource "keycloak_openid_group_membership_protocol_mapper" "trino_groups" {
  realm_id   = keycloak_realm.nda.id
  client_id  = keycloak_openid_client.trino.id
  name       = "groups"
  claim_name = "groups"
  full_path  = false
}

# Written where the marketplace tooling and the generated Trino config can read
# it. Gitignored, like every other generated credential.
resource "local_sensitive_file" "trino_oidc" {
  filename        = "${local.opa_dir}/trino_oidc.json"
  file_permission = "0600"
  content = jsonencode({
    # The issuer as it appears inside the token. Keycloak derives this from its
    # configured hostname, NOT from the address the caller used, which is why
    # compose pins KC_HOSTNAME - otherwise a token fetched on the host and a
    # token fetched from a container disagree and one of them fails validation.
    issuer        = "${var.keycloak_public_url}/realms/${var.realm}"
    client_id     = keycloak_openid_client.trino.client_id
    client_secret = keycloak_openid_client.trino.client_secret
    # Browser-facing: the person's machine resolves these.
    auth_url  = "${var.keycloak_public_url}/realms/${var.realm}/protocol/openid-connect/auth"
    token_url = "${var.keycloak_public_url}/realms/${var.realm}/protocol/openid-connect/token"
    # Server-facing: the Trino container resolves these over the shared network.
    internal_token_url = "${var.keycloak_internal_url}/realms/${var.realm}/protocol/openid-connect/token"
    internal_jwks_url  = "${var.keycloak_internal_url}/realms/${var.realm}/protocol/openid-connect/certs"
  })
}
