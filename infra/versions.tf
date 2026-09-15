terraform {
  required_version = ">= 1.6"

  required_providers {
    # Official Keycloak provider: identity, groups and role mappings.
    keycloak = {
      source  = "keycloak/keycloak"
      version = "~> 5.0"
    }
    # Renders the OPA bundle, so entitlements are declared once and enforced
    # from the same declaration that creates the users.
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
