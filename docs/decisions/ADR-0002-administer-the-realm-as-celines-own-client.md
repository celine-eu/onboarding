# ADR-0002 — The realm is administered by celine's own client, not the dataspace's

**Date:** 2026-09-10
**Status:** superseded by [ADR-0004](ADR-0004-ask-the-provisioning-service-instead-of-administering-the-realm.md)

Supersedes [ADR-0001](ADR-0001-provision-logins-as-the-service.md), whose reasoning
about the administrator credential stands and whose choice of *which* service account
replaces it does not.

## Context

ADR-0001 replaced a Keycloak realm administrator's username and password with a
client-credentials token, and picked `DS_ONBOARDING_CLIENT_ID` — `svc-ds-onboarding` —
because the service already loaded it and no second secret was needed. It recorded, as a
consequence it did not act on, that this is a client the **dataspace** owns.

That consequence is the decision. `clients.ds-host.yaml` in `../celine-policies` says what
it is for in its own header: it adds celine's grants to clients the dataspace declares,
and declares none of its own. Administering users in *celine's* realm is not a grant that
belongs on a dataspace's credential. Two things follow that are worse than untidy:

- **Coupling.** Keycloak provisioning is deliberately usable with no dataspace at all —
  `DATASPACE_KEYCLOAK_ENABLED` is a separate gate, and a deployment that gives
  participants a login and joins no dataspace is supported. Reading the realm-admin
  credential from a dataspace client made that configuration depend on a secret it has no
  reason to hold.
- **Reach.** A leaked `svc-ds-onboarding` secret would have carried its dataspace grants
  *and* the ability to modify any user in celine's realm. Those are granted by different
  people, for different things.

## Decision

**Administer the realm as `OIDC_CLIENT_ID` — `svc-onboarding`, which celine declares and
owns.** Its service account holds `manage-users` and `view-users` on `realm-management`,
and nothing else. `svc-ds-onboarding` keeps exactly what it had: the identity registry,
the connector, the registry lookups.

`services/service_auth.py` therefore names two identities rather than one, and says which
is granted by whom. Neither grant is reachable with the other's secret.

**Scope the grant to a group where the realm allows it.** `manage-users` is realm-wide:
update, delete, password reset and disable on every user in the realm, operator accounts
and other services' users included. That is more than provisioning ever needs. Keycloak's
fine-grained admin permissions (26.x, a preview feature at the time of writing) scope a
service account to one group, which is the difference between "may manage participants"
and "may disable an operator". Where the feature is available it is used; where it is not,
the realm-wide roles are the floor and the gap is documented rather than forgotten.

## Consequences

**`OIDC_CLIENT_SECRET` becomes required wherever provisioning is enabled.** It was
optional — inbound token verification needs the issuer and the JWKS, not a secret — so a
deployment that verified tokens without one now has to set it, and startup says so instead
of waiting for somebody to press Approve.

**One client, two jobs.** `svc-onboarding` is also the audience oauth2-proxy mints onto
operator tokens. Audiences and service-account roles are separate mechanisms — a user's
token never carries a client's service-account roles — so this does not widen what an
operator's browser token can do. What it does mean is that rotating the realm
administration credential is a console-authentication event. A dedicated
`svc-onboarding-users` client would decouple the two and make "who may manage users in
this realm" answerable by reading one client's name; it was weighed and not taken, because
it introduces a second secret to reach a realm this service already authenticates to.
Whoever revisits it should start from that trade, not from the credential ADR-0001
replaced.

**The manual step in Keycloak moves but does not go away.** `../celine-policies` still has
no way to declare a service account's realm-management roles, so the two roles are granted
by hand and a realm rebuilt from `clients.yaml` alone comes back unable to provision
logins. Tracked as
[celine-policies#2](https://github.com/celine-eu/celine-policies/issues/2). The symptom is
legible — every Admin API call refused, `Keycloak user lookup failed (403)`, with the
cause named in the server log.
