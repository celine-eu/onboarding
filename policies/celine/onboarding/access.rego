# METADATA
# title: REC Onboarding Console Access Policy
# description: Authorises operator and service access to the /api/admin surface
# scope: package
# entrypoint: true
package celine.onboarding.access

import rego.v1

# =============================================================================
# REC ONBOARDING CONSOLE AUTHORIZATION
# =============================================================================
#
# Two subject types, authorised on different evidence — the same split
# `celine.grid.access` makes, for the same reason:
#
#   * Operators (humans) are authorised by **group membership**. Keycloak has
#     already verified which organization they belong to, so the organization is
#     the tenancy boundary and the group is the role. They carry no
#     `onboarding.*` scope at all.
#   * Service accounts have no organization membership, so a **scope** is the
#     only way for them to express intent.
#
# Groups exist at two levels and the difference is load-bearing:
#
#   * A **realm**-level group (`groups` claim) is a platform-wide grant — it
#     applies to every community on the deployment.
#   * An **organization**-level group (`organization.<alias>.groups`) grants the
#     capability for that community only.
#
# The wrapper in `security/policy.py` passes these separately and never merges
# them. Merging is what `celine.sdk.auth.jwt.extract_groups` does, and it is the
# wrong thing here: a `managers` badge inside community A would otherwise satisfy
# a realm-level check and authorise an action on community B.
#
# An action name that appears in neither table is denied. Adding an endpoint
# without adding its capability here therefore fails closed.
#
# =============================================================================

default allow := false

default reason := "unauthorized"

# ── capability tables ────────────────────────────────────────────────────────

# The role hierarchy (admins > managers > editors > viewers) is expanded here
# rather than computed, so the grant for any single action is one readable line.
required_groups := {
	"recs.read": {"admins", "managers", "editors", "viewers"},
	"submissions.read": {"admins", "managers", "editors", "viewers"},
	"audit.read": {"admins", "managers", "editors", "viewers"},
	"submissions.reveal": {"admins", "managers", "editors"},
	"submissions.write": {"admins", "managers", "editors"},
	"submissions.review": {"admins", "managers"},
	"enablement.retry": {"admins", "managers"},
	"export": {"admins", "managers"},
	# Erasing somebody and revoking their credential are not recoverable. They
	# are deliberately not reachable through `submissions.review`.
	"submissions.purge": {"admins"},
	"enablement.revoke": {"admins"},
}

# `onboarding.admin` satisfies every entry below through the shared matcher's
# admin-override rule, so it stays a superset — but a service account should
# hold the actions it calls, not the superset.
required_scopes := {
	"recs.read": {"onboarding.recs.read"},
	"submissions.read": {"onboarding.submissions.read"},
	"submissions.reveal": {"onboarding.submissions.reveal"},
	"submissions.write": {"onboarding.submissions.write"},
	"submissions.review": {"onboarding.submissions.review"},
	"submissions.purge": {"onboarding.submissions.purge"},
	"enablement.retry": {"onboarding.enablement.retry"},
	"enablement.revoke": {"onboarding.enablement.revoke"},
	"audit.read": {"onboarding.audit.read"},
	"export": {"onboarding.export"},
}

known_action if required_groups[input.action.name]

# ── subject helpers ──────────────────────────────────────────────────────────

is_service if data.celine.scopes.is_service

# A realm-level group grants the action everywhere, so no organization check.
granted_by_realm_group if {
	some g in required_groups[input.action.name]
	g in input.subject.groups
}

# An organization-level group grants the action only for that organization's
# communities. `claims.organization` is the caller's organization as resolved
# *against this request's target*, and `claims.org_groups` holds that
# organization's groups only.
granted_by_org_group if {
	input.subject.claims.organization != null
	input.subject.claims.organization == input.resource.attributes.organization
	some g in required_groups[input.action.name]
	g in input.subject.claims.org_groups
}

# ── rules ────────────────────────────────────────────────────────────────────

allow if {
	not is_service
	granted_by_realm_group
}

allow if {
	not is_service
	granted_by_org_group
}

allow if {
	is_service
	data.celine.scopes.has_any_scope(required_scopes[input.action.name])
}

# ── reasons ──────────────────────────────────────────────────────────────────
#
# One else-chain rather than independent rules: two `reason` rules matching the
# same request is a rego conflict error, not a precedence question.

reason := "granted by realm group" if {
	not is_service
	granted_by_realm_group
} else := "granted by organization group" if {
	not is_service
	granted_by_org_group
} else := "granted by service scope" if {
	is_service
	allow
} else := "authentication required" if {
	data.celine.scopes.is_anonymous
} else := "unknown action — no capability is declared for it" if {
	not known_action
} else := "service is missing a scope granting this action" if {
	is_service
} else := "caller belongs to a different organization than this community" if {
	input.subject.claims.organization != input.resource.attributes.organization
} else := "no group grants this action"
