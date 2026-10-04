# ADR-0016 — The platform level is the realm role `platform-admin`, not a realm group

**Date:** 2026-10-03
**Status:** accepted

Narrows the "realm-level" grant that [ADR-0003](ADR-0003-provision-into-the-group-this-service-may-administer.md),
[ADR-0012](ADR-0012-areas-are-primary-substation-boundaries-owned-by-the-template.md) and
[ADR-0014](ADR-0014-registry-sync-sets-up-the-community-through-the-provisioning-reconcile.md)
rely on. Their decisions stand; read "realm `admins`", "realm-level" and "realm admin" in them
as the realm role `platform-admin`. Follows the platform decision in celine-policies
(ADR-0012 there, "a platform administrator is a realm role").

## Context

`access.rego` had two sources of grant. An organization's own groups (`admins > managers >
editors > viewers`) granted on that organization's REC. The **realm** groups `/admins` and
`/managers` (`platform_groups`) granted on every REC, and `recs.write` was reachable from
realm `admins` only.

Realm groups were designed for one Keycloak per organization. The platform now runs many
organizations in one realm, and each organization has groups with **the same names**. A token
therefore carried `admins` at two levels with two meanings. Onboarding read the two apart, but
every other reader that flattened them (the SDK's `extract_groups`) or guessed between `admins`
and `/admins` let a community's own administrators act as the platform's. Realm `managers` was
also a platform operator here, a middle level no one wanted.

## Decision

**Exactly two levels, and nothing in between (REQ-0030).**

- **Platform:** the Keycloak realm **role** `platform-admin`, read from the verified access
  token's `realm_access.roles`. It is the only platform-wide grant: every capability on every
  REC, `recs.write`, `recs.drift` and the deployment-wide actions included. It is a name no
  organization group carries.
- **Organization:** an organization's own groups, valid only inside that organization, and only
  when it is typed `rec`.

**A realm group grants nothing.** The top-level `groups` claim is not read for authorization,
`/admins` included. No other realm role (`admin`, `manager`, …) and no client role grants
anything. `platform_groups`, `realm_required_groups` and the realm-group rules are removed from
`access.rego`; `platform_only_actions` (`recs.write`) is granted by the role alone.

**The policy input carries the levels apart.** Realm roles travel in `input.subject.roles`,
`input.subject.groups` is always empty, and only the matched organization's groups travel in
`input.subject.claims.org_groups`. `security/policy.py` builds that input through the SDK's
`Subject(roles=…)` and `PolicyEngine.build_input_dict` (celine-sdk 2.0.0), which emits
`subject.roles` beside `subject.groups` and never merges them.

**Person or service is decided by `is_service_account`**, never by whether a token carries a
group. A service is authorised by scope only; the role is read for people (and for the operator
of a delegated action).

No compatibility path is kept for the old realm groups. The realm converges to the new shape
(celine-policies `keycloak bootstrap` removes the retired groups and roles), and the change
ships whole.

## Consequences

- **A platform administrator must be granted the role**, not put in `/admins`. On a realm that
  has not converged, a former `/admins` member is refused everywhere a platform grant was needed
  (registry sync included) until the role is assigned.
- **Realm `managers` lose their cross-community reach.** Operating a community now needs
  membership of that community's organization.
- **`GET /api/admin/me` reports `platform_roles`** instead of `realm_groups`; `onboarding-cli
  whoami` prints them. Both are diagnostic only.
- **The service needs celine-sdk 2.0.0** (`realm_roles`, `is_platform_admin`; `realm_groups` and
  `extract_groups` removed). The declared floor is raised when 2.0.0 is published.
- **Real-token tests** (`tests/real_tokens`, `task test:real-tokens`) prove against a local
  Keycloak that an organization's `admins` is not a platform admin, that the role holder is,
  and that a realm group still present in a token grants nothing.
