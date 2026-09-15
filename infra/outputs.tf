output "realm" {
  description = "Keycloak realm holding NDA identities"
  value       = keycloak_realm.nda.realm
}

output "dashboard_client_id" {
  value = keycloak_openid_client.dashboard.client_id
}

output "dashboard_client_secret" {
  description = "Confidential client secret for the dashboard"
  value       = keycloak_openid_client.dashboard.client_secret
  sensitive   = true
}

output "stakeholder_groups" {
  description = "Group -> what it may see and export"
  value = {
    for name, group in var.stakeholder_groups : name => {
      processes = group.processes
      layers    = length(group.layers) > 0 ? group.layers : ["(no export)"]
      indicators = contains(group.indicators, "*") ? "all" : format("%d indicators", length(group.indicators))
      dashboard = group.full_dashboard ? "full" : "restricted"
    }
  }
}

output "trino_roles" {
  description = "Keycloak group -> the marketplace role Trino resolves its members to"
  value       = { for name, group in var.stakeholder_groups : name => group.trino_role }
}

output "users_by_group" {
  description = "Which demonstration accounts belong to which group"
  value = {
    for group in keys(var.stakeholder_groups) :
    group => [for name, user in var.stakeholder_users : name if user.group == group]
  }
}

output "credentials_file" {
  description = "Generated demonstration passwords (gitignored)"
  value       = local_sensitive_file.credentials.filename
}
