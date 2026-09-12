# ADR-0003 — Provisioning reaches one group, and finds people by scanning it

**Date:** 2026-09-10
**Status:** superseded by [ADR-0004](ADR-0004-ask-the-provisioning-service-instead-of-administering-the-realm.md)

Completes [ADR-0002](ADR-0002-administer-the-realm-as-celines-own-client.md), which chose
*which* client administers the realm and said the grant should be scoped to a group "where
the realm allows it". It always does, and this is what it costs.

## Context

`../celine-policies` now declares `svc-onboarding`'s realm administration in `clients.yaml`
as a group-scoped fine-grained permission over `/participants`, in place of the realm-wide
`manage-users` / `view-users` this service was hand-granted
([onboarding#3](https://github.com/celine-eu/onboarding/issues/3)). The declaration was
inert, because two calls here cannot run under it: `POST /users` with no `groups` key, and
the realm-wide `GET /users?username=`.

ADR-0002 also recorded something that is no longer true. It called fine-grained admin
permissions "26.x, a preview feature at the time of writing" and kept realm-wide roles as
the documented floor. On Keycloak 26.6.0 `ADMIN_FINE_GRAINED_AUTHZ_V2` is enabled by
default and `ADMIN_FINE_GRAINED_AUTHZ` is deprecated: the narrow grant is always available,
and there is no floor to fall back to.

Four things were measured on that version, on throwaway realms, one grant per realm.
Two of them contradict the shape the issue proposed:

| | |
|---|---|
| `GET /groups/{id}/members?search=…` | 200, and the filter is **ignored** — every member comes back for every query, `exact=true` included |
| The group's id, under `manage-members` + `manage-membership` + `view-members` | 403 from `group-by-path`, `/groups`, `/groups?search=` and `/groups/{id}` alike. The **`view`** scope is what opens `group-by-path` |
| `POST /users` naming a group that does not exist | **403**, indistinguishable from a missing grant |
| A duplicate name or address | **409** whichever side of the group its owner is on — uniqueness is checked before containment |

## Decision

**Create into the group, and let Keycloak's `409` be the duplicate check.** The order
inverts. Since the only lookup available is a scan of the whole group, running it before
every creation would page a hundred participants at a time to discover what is almost
always a new user. So `POST /users` carries `groups: [<participants group>]` and goes
first; a `409` triggers a paged scan of the group, matched here on username *or* email.

**A duplicate outside the group fails the enablement step, and says what to do.** Under
this grant such an account cannot be read, adopted, or moved into the group — it is not
merely unfound, it is unreachable. Nothing can be done automatically, so the step error
carries the human act: an account already uses this address and is not in the group; add it
there, or resolve the collision. The alternative — asking `celine-policies` for realm-wide
`Users: view` so the old search keeps working — was rejected in that repository and again
here: it is read-only and it works, and it lets the service read every account in the
realm, which is what the scoping exists to prevent.

**The group is a prerequisite, not a mode.** `DATASPACE_KEYCLOAK_PARTICIPANTS_GROUP`
defaults to `/participants` and is always used. There is deliberately no "create in no
group" fallback, because that is precisely the act the grant refuses; a realm that has not
been synced fails visibly instead. `keycloak sync` creates the group from the same
declaration that grants the permission over it, so the prerequisite is satisfied wherever
the declaration is applied.

**Startup refuses `admins`, `managers`, `editors` and `viewers` as that group.** They are
the *operator* hierarchy, and `policies/celine/onboarding/access.rego` reads a realm-level
one as a platform-wide grant with no organization check. Configuring one would enrol every
participant as an operator of every community on the deployment. `viewers` is not a safe
floor: least privilege among operators is not no privilege, and it still reads every REC's
submissions and audit trail. `participants` is safe precisely because it appears in no
capability table.

> **Update, 2026-09-11.** The hole that paragraph describes is now closed at its source:
> `access.rego` reads a realm-level badge as platform-wide only for `admins` and `managers`,
> so a realm `viewers` grants nothing anywhere. The refusal is **unchanged** and still covers
> all four names — a group that grants nothing today is one capability-table edit away from
> granting something, and a participant is not an operator of any tier. What changed is that
> the refusal is no longer the only thing standing between a misconfiguration and every
> participant reading every REC.

## Consequences

**Adoption is now bounded by the group.** A login this service created is found again as
before — a re-approval or a retried enablement adopts it and keeps the username Keycloak
holds, which is what `Member.user_id` needs. A login created by anything else is not:
`celine-policies`' `sync-users` files its participants under `derive_username(key)` with no
administered group, so every account it made is on the far side of the `409` until somebody
puts it in the group. Reported there.

**The grant needs a fourth scope.** `view`, alongside the three member scopes, or the
service cannot resolve the group id and adoption cannot work at all. It is resolved lazily,
so a deployment whose declaration lacks it still provisions new logins and fails only when
it has to adopt somebody — with a 403 whose log line names the missing scope. Reported to
`celine-policies`.

**A missing group and a missing grant look the same on a creation.** Both are 403. Only
`group-by-path` tells them apart, with a 404, and that is reported as a misconfiguration —
the operator holding the review queue is told a platform operator has to act, and the
detail goes to the log.

**Provisioning costs one call in the ordinary case**, down from two: no lookup precedes the
creation. The uncommon case — somebody who already has a login — costs a page of members per
hundred participants in the group, at about 22 ms a page on the development realm.
