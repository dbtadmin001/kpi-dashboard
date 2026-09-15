# Authorization for the NDA data serving layer.
#
# The policy is static; the entitlements it reads (data.entitlements) are
# rendered by Terraform from the same map that creates the Keycloak groups, so
# identity and authorization cannot drift apart.
#
# Deny by default. Every rule below grants; nothing revokes. That ordering
# matters: a user with no recognised group gets an empty entitlement, not a
# permissive one.
package nda.authz

import rego.v1

default allow := false

# The caller's groups, intersected with the groups the policy knows about.
# An unknown group contributes nothing rather than being treated as a wildcard.
known_groups contains group if {
	some group in input.groups
	data.entitlements[group]
}

entitlements contains data.entitlements[group] if {
	some group in known_groups
}

# ---------------------------------------------------------------------------
# Effective entitlement: the union over every group the user belongs to.
# ---------------------------------------------------------------------------
processes contains process if {
	some entitlement in entitlements
	some process in entitlement.processes
}

layers contains layer if {
	some entitlement in entitlements
	some layer in entitlement.layers
}

formats contains format if {
	some entitlement in entitlements
	some format in entitlement.formats
}

# Needs an explicit default: an undefined rule would make the whole entitlement
# object below undefined, so a restricted group would read as "no answer"
# rather than "restricted dashboard".
default full_dashboard := false

full_dashboard if {
	some entitlement in entitlements
	entitlement.full_dashboard
}

row_limit := limit if {
	limits := [entitlement.row_limit | some entitlement in entitlements]
	count(limits) > 0
	limit := max(limits)
}

else := 0

# "*" means every indicator; otherwise the explicit allow-list applies.
unrestricted_indicators if {
	some entitlement in entitlements
	"*" in entitlement.indicators
}

indicators contains indicator if {
	not unrestricted_indicators
	some entitlement in entitlements
	some indicator in entitlement.indicators
}

# ---------------------------------------------------------------------------
# Decisions the serving layer asks for.
# ---------------------------------------------------------------------------

# May this caller see this regulatory process at all?
allow_process if {
	input.action == "view_process"
	input.process in processes
}

# May this caller see this specific indicator?
allow_indicator if {
	input.action == "view_indicator"
	input.process in processes
	unrestricted_indicators
}

allow_indicator if {
	input.action == "view_indicator"
	input.process in processes
	input.indicator in indicators
}

# May this caller export this table, in this layer, in this format?
allow_export if {
	input.action == "export"
	input.layer in layers
	input.format in formats
	process_of_table(input.table) in processes
}

# Bronze holds the raw CDC envelope, including every intermediate state and the
# pre-masking identifiers. Only groups explicitly granted bronze may touch it,
# which is why the layer check above is an allow-list rather than a hierarchy.
allow if allow_process

allow if allow_indicator

allow if allow_export

# A table name carries its process prefix (ma_, ct_, gmp_, fact_ma_, ...).
process_of_table(table) := process if {
	some candidate in ["ma", "ct", "gmp"]
	parts := split(table, "_")
	some part in parts
	part == candidate
	process := upper(candidate)
}

# The full entitlement, returned in one call so the dashboard can render the
# right tabs without asking a question per widget.
entitlement := {
	"groups": known_groups,
	"processes": processes,
	"layers": layers,
	"formats": formats,
	"indicators": indicator_view,
	"full_dashboard": full_dashboard,
	"row_limit": row_limit,
	"can_export": count(layers) > 0,
}

indicator_view := "*" if unrestricted_indicators

else := indicators
