provider "keycloak" {
  client_id = "admin-cli"
  username  = var.keycloak_admin
  password  = var.keycloak_admin_password
  url       = var.keycloak_url
  # Dev-mode Keycloak takes a moment to accept admin calls after a restart.
  initial_login = false
}

resource "keycloak_realm" "nda" {
  realm        = var.realm
  enabled      = true
  display_name = "National Drug Authority"

  # Short-lived tokens: the dashboard re-authorises often, so a revoked or
  # regrouped user loses access in minutes rather than at next logout.
  access_token_lifespan       = "10m"
  sso_session_idle_timeout    = "30m"
  sso_session_max_lifespan    = "8h"
  login_with_email_allowed    = true
  registration_allowed        = false
  reset_password_allowed      = false
  verify_email                = false

  password_policy = "length(12) and upperCase(1) and lowerCase(1) and digits(1) and notUsername"
}

# The dashboard authenticates users directly (Streamlit has no browser redirect
# handler), so this client is confidential with direct access grants enabled.
resource "keycloak_openid_client" "dashboard" {
  realm_id  = keycloak_realm.nda.id
  client_id = "nda-dashboard"
  name      = "NDA regulatory dashboard"
  enabled   = true

  access_type                  = "CONFIDENTIAL"
  standard_flow_enabled        = true
  direct_access_grants_enabled = true
  service_accounts_enabled     = false

  valid_redirect_uris = ["${var.dashboard_url}/*"]
  web_origins         = [var.dashboard_url]
}

# Put group membership in the token so the serving layer can authorise without
# a second round trip to Keycloak on every request.
resource "keycloak_openid_group_membership_protocol_mapper" "groups" {
  realm_id   = keycloak_realm.nda.id
  client_id  = keycloak_openid_client.dashboard.id
  name       = "groups"
  claim_name = "groups"
  full_path  = false
}
