variable "keycloak_url" {
  description = "Keycloak base URL"
  type        = string
  default     = "http://127.0.0.1:8180"
}

variable "keycloak_admin" {
  description = "Bootstrap admin username"
  type        = string
  default     = "admin"
}

variable "keycloak_admin_password" {
  description = "Bootstrap admin password (KEYCLOAK_ADMIN_PASSWORD in streaming/.env)"
  type        = string
  sensitive   = true
}

# The URL Keycloak puts in the `iss` claim and sends browsers to. It must match
# KC_HOSTNAME in streaming/compose.yaml, or Trino rejects every token as issued
# by someone else.
variable "keycloak_public_url" {
  description = "Keycloak as a person's browser reaches it"
  type        = string
  default     = "http://localhost:8180"
}

# How the Trino container reaches Keycloak for JWKS and token exchange. Different
# address, same issuer - Keycloak's backchannel-dynamic setting allows that.
variable "keycloak_internal_url" {
  description = "Keycloak as another container reaches it"
  type        = string
  default     = "http://nda-keycloak:8180"
}

variable "trino_token_lifespan_seconds" {
  description = "Access token lifetime for SQL clients. 12h suits a desktop tool; shorten it for production."
  type        = string
  default     = "43200"
}

variable "trino_https_port" {
  description = "Host port for the marketplace coordinator's TLS listener"
  type        = number
  default     = 8443
}

variable "realm" {
  description = "Realm holding NDA dashboard identities"
  type        = string
  default     = "nda"
}

variable "dashboard_url" {
  description = "Redirect target for the dashboard client"
  type        = string
  default     = "http://127.0.0.1:8501"
}

# ---------------------------------------------------------------------------
# Entitlements.
#
# This map is the single source of truth: it creates the Keycloak groups, renders
# the OPA data document the serving layer authorises against, AND carries the
# marketplace role that Trino enforces. A group cannot exist in identity without
# a matching policy, or gain data access without an identity, because all three
# are generated from this one declaration.
#
#   processes  - which regulatory processes the stakeholder may see at all
#   layers     - medallion layers they may export from ([] means no export)
#   indicators - "*" for every KPI, or an explicit allow-list
#   row_limit  - hard cap on a single export
#   trino_role - the marketplace role Trino resolves this group's members to.
#                Must be one of ROLES in marketplace/products.py. Published as a
#                group attribute so the Trino group provider reads policy from
#                the directory rather than from a second, drifting list.
# ---------------------------------------------------------------------------
variable "stakeholder_groups" {
  description = "Stakeholder type -> what that type may see and export"
  type = map(object({
    description    = string
    processes      = list(string)
    layers         = list(string)
    indicators     = list(string)
    formats        = list(string)
    full_dashboard = bool
    row_limit      = number
    trino_role     = string
  }))

  default = {
    data-engineering = {
      description    = "Platform engineers: every process, every medallion layer, raw export"
      processes      = ["MA", "CT", "GMP"]
      layers         = ["nda_bronze", "nda_silver", "nda_gold"]
      indicators     = ["*"]
      formats        = ["csv", "xlsx"]
      full_dashboard = true
      row_limit      = 1000000
      trino_role     = "data_engineer"
    }
    ma-analysts = {
      description    = "Marketing Authorization analysts: MA only, curated layers"
      processes      = ["MA"]
      layers         = ["nda_silver", "nda_gold"]
      indicators     = ["*"]
      formats        = ["csv", "xlsx"]
      full_dashboard = false
      row_limit      = 250000
      trino_role     = "analyst"
    }
    ct-analysts = {
      description    = "Clinical Trials analysts: CT only, curated layers"
      processes      = ["CT"]
      layers         = ["nda_silver", "nda_gold"]
      indicators     = ["*"]
      formats        = ["csv", "xlsx"]
      full_dashboard = false
      row_limit      = 250000
      trino_role     = "analyst"
    }
    gmp-analysts = {
      description    = "GMP inspectorate analysts: GMP only, curated layers"
      processes      = ["GMP"]
      layers         = ["nda_silver", "nda_gold"]
      indicators     = ["*"]
      formats        = ["csv", "xlsx"]
      full_dashboard = false
      row_limit      = 250000
      trino_role     = "analyst"
    }
    # Modelling work needs silver as well as gold, but still reads entity_id
    # masked - a wider slice of the lakehouse, not a wider slice of the people.
    data-science = {
      description    = "Data scientists: every process, curated and conformed layers"
      processes      = ["MA", "CT", "GMP"]
      layers         = ["nda_silver", "nda_gold"]
      indicators     = ["*"]
      formats        = ["csv", "xlsx"]
      full_dashboard = true
      row_limit      = 500000
      trino_role     = "data_scientist"
    }
    # Leadership reads the certified products and nothing underneath them: the
    # numbers on the slide, without a route to the physical tables behind them.
    #
    # No layers therefore means no export formats and no row limit either. The
    # dashboard and Trino are two enforcement points over the same data, and a
    # group that cannot SELECT nda_gold must not be able to download it instead -
    # otherwise the export button is a way around the access rules.
    executive = {
      description    = "Executive and directorate: all processes, certified products only"
      processes      = ["MA", "CT", "GMP"]
      layers         = []
      indicators     = ["*"]
      formats        = []
      full_dashboard = true
      row_limit      = 0
      trino_role     = "business_user"
    }
    # Public transparency: service-delivery timeliness only. Compliance and CAPA
    # indicators are deliberately withheld - they report failure rates for small,
    # potentially identifiable cohorts of facilities and trials, which is a
    # re-identification and reputational risk the timeliness measures do not carry.
    public = {
      description    = "Public transparency: two timeliness indicators per process, no export"
      processes      = ["MA", "CT", "GMP"]
      layers         = []
      indicators = [
        "pct_new_apps_evaluated_on_time",
        "pct_granted_within_90_days",
        "pct_new_apps_evaluated_on_time_ct",
        "pct_registry_submissions_on_time",
        "pct_facilities_inspected_on_time",
        "pct_reports_published_on_time",
      ]
      formats        = []
      full_dashboard = false
      row_limit      = 0
      trino_role     = "business_user"
    }
    # Engine identities, not people: the Trino view owners and the dashboard's
    # serving principal. They are in the directory so that "who is an
    # administrator" has exactly one answer an auditor can read.
    platform-services = {
      description    = "Engine service identities: Trino view owners and the serving principal"
      processes      = ["MA", "CT", "GMP"]
      layers         = ["nda_bronze", "nda_silver", "nda_gold"]
      indicators     = ["*"]
      formats        = ["csv", "xlsx"]
      full_dashboard = true
      row_limit      = 1000000
      trino_role     = "administrator"
    }
  }
}

variable "stakeholder_users" {
  description = "Seed accounts. Day-to-day joiners are added in the Keycloak console, not here."
  type = map(object({
    group      = string
    first_name = string
    last_name  = string
    email      = string
  }))

  default = {
    "dana.okello"      = { group = "data-engineering", first_name = "Dana", last_name = "Okello", email = "dana.okello@nda.example" }
    "brian.mugisha"    = { group = "data-engineering", first_name = "Brian", last_name = "Mugisha", email = "brian.mugisha@nda.example" }
    "alice.nakato"     = { group = "ma-analysts", first_name = "Alice", last_name = "Nakato", email = "alice.nakato@nda.example" }
    "peter.ssemwanga"  = { group = "ma-analysts", first_name = "Peter", last_name = "Ssemwanga", email = "peter.ssemwanga@nda.example" }
    "grace.auma"       = { group = "ct-analysts", first_name = "Grace", last_name = "Auma", email = "grace.auma@nda.example" }
    "david.kato"       = { group = "ct-analysts", first_name = "David", last_name = "Kato", email = "david.kato@nda.example" }
    "sarah.namugga"    = { group = "gmp-analysts", first_name = "Sarah", last_name = "Namugga", email = "sarah.namugga@nda.example" }
    "james.opio"       = { group = "gmp-analysts", first_name = "James", last_name = "Opio", email = "james.opio@nda.example" }
    "sam.scientist"    = { group = "data-science", first_name = "Sam", last_name = "Scientist", email = "sam.scientist@nda.example" }
    "chief.director"   = { group = "executive", first_name = "Chief", last_name = "Director", email = "chief.director@nda.example" }
    "public.viewer"    = { group = "public", first_name = "Public", last_name = "Viewer", email = "public.viewer@nda.example" }

    # Engine identities. marketplace_owner owns the certified views and
    # nda_dashboard owns the nda_gold.all_* views they read, so both need the
    # administrator role for Trino's DEFINER check to pass down the chain.
    "marketplace_owner" = { group = "platform-services", first_name = "Marketplace", last_name = "Owner", email = "marketplace_owner@nda.example" }
    "nda_dashboard"     = { group = "platform-services", first_name = "Dashboard", last_name = "Service", email = "nda_dashboard@nda.example" }
    "admin"             = { group = "platform-services", first_name = "Platform", last_name = "Administrator", email = "admin@nda.example" }
  }
}
