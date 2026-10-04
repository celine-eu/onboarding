# Platform administrator

Who may act on every community the deployment serves, and who may act on one. There are
exactly two levels of grant on `/api/admin/**`, and nothing in between: the realm role
`platform-admin`, and an organization's own groups inside that organization. The rest of the
authorization model is described in [authorization.md](../authorization.md).

---

### REQ-0030 — the realm role `platform-admin` is the only platform-wide grant; a realm group grants nothing

- **A platform administrator is a person whose access token carries the realm role
  `platform-admin`** in `realm_access.roles`. It grants every capability on every REC,
  including `recs.write` (REQ-0009) and `recs.drift` (REQ-0015), and the deployment-wide
  actions that name no community (such as `POST /api/admin/recs/reload`).
- **Nothing else is platform-wide.** A realm group grants nothing: the top-level `groups`
  claim is not read for authorization, whatever it holds (`/admins`, `/managers`, `admins`,
  …). No other realm role grants anything either, `admin` and `manager` included, and no
  client role (`resource_access`) does.
- **An organization's own groups grant only on that organization's REC**, and only when the
  organization is typed `rec`. An organization's `admins` is not a platform administrator:
  it reaches neither another community, nor `recs.write`, nor a deployment-wide action.
- **The policy input keeps the two levels apart.** The caller's realm roles travel in
  `input.subject.roles`; `input.subject.groups` is always empty; the groups of the one
  organization the request concerns travel in `input.subject.claims.org_groups`, and no
  other organization's groups are passed.
- **A service account is not made a platform administrator by a role.** A service is
  authorised by its scopes only; the role is read for people. A delegated action
  (`members.invite`) still needs a service acting for the operator, and an acting operator
  who holds `platform-admin` qualifies.
- **Whether a caller is a person or a service is decided by the SDK's
  `is_service_account`**, never by whether the token carries a realm group.
- **`GET /api/admin/me` reports `platform_roles`** (the caller's realm roles) in place of
  the former `realm_groups`, and `onboarding-cli whoami` prints them. Both are diagnostic:
  the console shows and hides by the per-REC capabilities, never by this list.
