# One Keycloak group per stakeholder type, created from the same map that
# renders the OPA policy data. Adding a stakeholder type is a single edit.
resource "keycloak_group" "stakeholder" {
  for_each = var.stakeholder_groups

  realm_id = keycloak_realm.nda.id
  name     = each.key

  attributes = {
    description = each.value.description
    processes   = join(",", each.value.processes)
    layers      = join(",", each.value.layers)
    # Read by marketplace/identity.py to build Trino's group file. This is why
    # membership is never restated in Python: the group carries its own role.
    trino_role = each.value.trino_role
  }
}

# A realm role per group makes the entitlement visible in the token as a role
# as well as a group, which is what most downstream tools expect to read.
resource "keycloak_role" "stakeholder" {
  for_each = var.stakeholder_groups

  realm_id    = keycloak_realm.nda.id
  name        = "nda-${each.key}"
  description = each.value.description
}

resource "keycloak_group_roles" "stakeholder" {
  for_each = var.stakeholder_groups

  realm_id = keycloak_realm.nda.id
  group_id = keycloak_group.stakeholder[each.key].id
  role_ids = [keycloak_role.stakeholder[each.key].id]
}

# Demonstration passwords are generated, never hard-coded, and surfaced only
# through a sensitive output written to a gitignored file.
resource "random_password" "user" {
  for_each = var.stakeholder_users

  length           = 20
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 1
  override_special = "!#%-_"
}

resource "keycloak_user" "stakeholder" {
  for_each = var.stakeholder_users

  realm_id   = keycloak_realm.nda.id
  username   = each.key
  enabled    = true
  email      = each.value.email
  first_name = each.value.first_name
  last_name  = each.value.last_name

  # Demonstration accounts: no forced reset, so they can be handed over and used.
  initial_password {
    value     = random_password.user[each.key].result
    temporary = false
  }
}

resource "keycloak_user_groups" "stakeholder" {
  for_each = var.stakeholder_users

  realm_id = keycloak_realm.nda.id
  user_id  = keycloak_user.stakeholder[each.key].id
  group_ids = [
    keycloak_group.stakeholder[each.value.group].id
  ]
}
