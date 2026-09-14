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
#     applies to every community on the deployment. Because it is that wide,
#     only `platform_groups` below carry one; a realm `editors` or `viewers`
#     badge grants nothing anywhere.
#   * An **organization**-level group (`organization.<alias>.groups`) grants the
#     capability for that community only, and only when that organization is
#     typed `rec`. The console administers RECs; an organization of another kind
#     is not one, whatever its members are called.
#
# The wrapper in `security/policy.py` passes these separately and never merges
# them. Merging is what `celine.sdk.auth.jwt.extract_groups` does, and it is the
# wrong thing here: a `managers` badge inside community A would otherwise satisfy
# a realm-level check and authorise an action on community B.
#
# An action name that appears in neither table is denied. Adding an endpoint
# without adding its capability here therefore fails closed.
#
# A **delegated** action is a third case, and neither subject type reaches it on
# its own. It is allowed only to a service holding its scope that also presents
# the verified token of the operator it acts for, and only when that operator
# holds a group granting the action by the rules above. The operator is
# `input.environment.actor`: the SDK's engine serialises a fixed input shape, and
# `environment` is its free-form request slot. So a manager's own token cannot
# call it, and neither can a service alone, `onboarding.admin` included: email
# follows a person's decision, taken on the community dashboard.
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
	# Delegated: the group is the acting operator's, never the caller's.
	"members.invite": {"admins", "managers"},
}

# Which of those groups mean anything at **realm** level. A realm badge is a
# grant over every community on the deployment with no organization check, so it
# is the platform-operator role and not merely the top of the tier list: the two
# read-only tiers are excluded from it entirely.
#
# This is an intersection with the table above, not a second table. A realm
# `managers` still cannot purge a submission or revoke a credential, because
# those two actions name only `admins` — the tier keeps deciding *which* actions,
# and this set decides *whether the realm level applies at all*.
#
# `editors` and `viewers` keep their meaning one level down, where a read-only
# member of one REC belongs.
platform_groups := {"admins", "managers"}

# The organization type that owns a REC, as `celine-policies keycloak sync-orgs`
# writes it from the owner's `organization.role`. An organization carrying no
# type grants nothing: absent is exactly the state of a realm that was never
# synced, and treating it as "probably a REC" would make the check bypassable by
# omission.
rec_organization_type := "rec"

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
	"members.invite": {"onboarding.members.invite"},
}

# Actions reachable only by a service acting for a verified operator. See the
# header.
delegated_actions := {"members.invite"}

known_action if required_groups[input.action.name]

is_delegated if input.action.name in delegated_actions

# ── subject helpers ──────────────────────────────────────────────────────────

is_service if data.celine.scopes.is_service

# The operator a delegated call acts for, built from their verified token by the
# same code that builds `input.subject`.
actor := input.environment.actor

has_actor if actor.type == "user"

# A realm-level group grants the action everywhere, so no organization check —
# and for that reason only a platform group qualifies.
realm_group_grants(principal) if {
	some g in required_groups[input.action.name]
	g in platform_groups
	g in principal.groups
}

# An organization-level group grants the action only for that organization's
# communities, and only when that organization is a REC. `claims.organization` is
# the caller's organization as resolved *against this request's target*,
# `claims.org_groups` holds that organization's groups only, and `claims.org_type`
# its type attribute — read from the flattened `type` key a real token carries,
# falling back to the nested `attributes.type` a fixture may use.
org_group_grants(principal) if {
	principal.claims.organization != null
	principal.claims.organization == input.resource.attributes.organization
	principal.claims.org_type == rec_organization_type
	some g in required_groups[input.action.name]
	g in principal.claims.org_groups
}

granted_by_realm_group if realm_group_grants(input.subject)

granted_by_org_group if org_group_grants(input.subject)

# The acting operator is judged by exactly the rules the caller would be.
actor_granted if {
	has_actor
	realm_group_grants(actor)
}

actor_granted if {
	has_actor
	org_group_grants(actor)
}

# ── rules ────────────────────────────────────────────────────────────────────

allow if {
	not is_service
	not is_delegated
	granted_by_realm_group
}

allow if {
	not is_service
	not is_delegated
	granted_by_org_group
}

allow if {
	is_service
	not is_delegated
	data.celine.scopes.has_any_scope(required_scopes[input.action.name])
}

allow if {
	is_service
	is_delegated
	data.celine.scopes.has_any_scope(required_scopes[input.action.name])
	actor_granted
}

# ── reasons ──────────────────────────────────────────────────────────────────
#
# One else-chain rather than independent rules: two `reason` rules matching the
# same request is a rego conflict error, not a precedence question.

reason := "granted by realm group" if {
	not is_service
	not is_delegated
	granted_by_realm_group
} else := "granted by organization group" if {
	not is_service
	not is_delegated
	granted_by_org_group
} else := "granted by service scope, acting for an operator" if {
	is_service
	is_delegated
	allow
} else := "granted by service scope" if {
	is_service
	allow
} else := "authentication required" if {
	data.celine.scopes.is_anonymous
} else := "unknown action — no capability is declared for it" if {
	not known_action
} else := "service is missing a scope granting this action" if {
	is_service
	not data.celine.scopes.has_any_scope(required_scopes[input.action.name])
} else := "a delegated action is reachable only through a service acting for an operator" if {
	is_delegated
	not is_service
} else := "a delegated action needs an acting operator" if {
	is_delegated
	not has_actor
} else := "the acting operator holds no group granting this action" if {
	is_delegated
} else := "a realm group is not a platform-wide grant — only admins and managers are" if {
	some g in required_groups[input.action.name]
	g in input.subject.groups
} else := "caller belongs to a different organization than this community" if {
	input.subject.claims.organization != input.resource.attributes.organization
} else := "the caller's organization is not typed as a REC" if {
	input.subject.claims.organization != null
	input.subject.claims.org_type != rec_organization_type
} else := "no group grants this action"
