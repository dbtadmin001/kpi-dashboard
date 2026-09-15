# The OPA bundle. Rendering it from the same map that creates the Keycloak
# groups is the point of this file: identity and authorization are one
# declaration, so a group can never exist without a policy or vice versa.
locals {
  opa_dir = "${path.module}/generated"

  entitlements = {
    for name, group in var.stakeholder_groups : name => {
      description    = group.description
      processes      = group.processes
      layers         = group.layers
      indicators     = group.indicators
      formats        = group.formats
      full_dashboard = group.full_dashboard
      row_limit      = group.row_limit
      # Carried into the policy document so "what can this group reach in the
      # warehouse" is answerable from the same place as "what can it see in the
      # dashboard", rather than requiring a second lookup in Trino's config.
      trino_role = group.trino_role
    }
  }
}

resource "local_file" "opa_data" {
  filename        = "${local.opa_dir}/data.json"
  file_permission = "0644"
  content = jsonencode({
    entitlements = local.entitlements
  })
}

# OPA loads a directory; keeping one copy of the policy beside the generated
# data means the container mounts a single path and Terraform owns all of it.
resource "local_file" "opa_policy" {
  filename        = "${local.opa_dir}/authz.rego"
  file_permission = "0644"
  content         = file("${path.module}/policy/authz.rego")
}

# Demonstration credentials, for handing the accounts over. Gitignored.
resource "local_sensitive_file" "credentials" {
  filename        = "${local.opa_dir}/credentials.json"
  file_permission = "0600"
  content = jsonencode({
    realm         = var.realm
    keycloak_url  = var.keycloak_url
    client_id     = keycloak_openid_client.dashboard.client_id
    client_secret = keycloak_openid_client.dashboard.client_secret
    users = {
      for name, user in var.stakeholder_users : name => {
        password = random_password.user[name].result
        group    = user.group
        name     = "${user.first_name} ${user.last_name}"
      }
    }
  })
}
