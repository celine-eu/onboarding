# ADR-0001 — Participant logins are provisioned by this service's own service account, never by a Keycloak administrator

**Date:** 2026-09-10
**Status:** superseded by [ADR-0002](ADR-0002-administer-the-realm-as-celines-own-client.md)

## Context

Approving a submission provisions the participant's Keycloak login as step 1 of
enablement. Until now that call authenticated with `grant_type=password`, client
`admin-cli`, against the **master** realm — a human realm administrator's username and
password, read from `DATASPACE_KEYCLOAK_ADMIN_USERNAME` and
`DATASPACE_KEYCLOAK_ADMIN_PASSWORD`.

Three things were wrong with that, and they compound.

**The credential is unbounded and the job is not.** Provisioning needs to create a user,
read one back, and refresh a profile — `manage-users` and `view-users` on one realm. A
master-realm administrator can do anything to every realm, including granting itself
more.

**It is held by the service that faces the public.** This app serves the anonymous
wizard, accepts uploads, and calls an external extraction provider. It is the process in
this platform most exposed to an untrusted input, and it was the one holding the realm
administrator's password.

**Nothing here needed it to be a person.** The app already authenticates as itself for
every other outbound call — `celine.sdk.auth.OidcClientCredentialsProvider`,
`DS_ONBOARDING_CLIENT_ID` against `OIDC_BASE_URL`. Keycloak provisioning was the one
caller that logged in as somebody instead. `../celine-policies` had already reached the
same conclusion for its own CLI: `keycloak bootstrap` exists to create a service account
with realm-management roles "instead of admin user credentials", and its client tries
client credentials first.

The failure that surfaced this was ordinary. A deployment enabled provisioning without
setting the credentials, so an operator pressing **Approve** was shown
`Login identity could not be provisioned: ValueError: DATASPACE_KEYCLOAK_ADMIN_USERNAME
is required` — a deployment's internal variable name, in a REC operator's browser, at
the one moment nothing could be done about it.

## Decision

**Provision as the service.** Keycloak Admin API calls carry the same service-account
token as every other outbound call. The client's service account is granted
`manage-users` and `view-users` on `realm-management`, and nothing else.

**Delete the password grant rather than demote it.** A fallback would keep the variables
alive in every `.env` copied from the example.

**Refuse to boot on a leftover.** `DATASPACE_KEYCLOAK_ADMIN_USERNAME`, `_PASSWORD` and
`_CLIENT_SECRET` are declared solely so startup can reject them, as `ADMIN_TOKEN` already
is. A credential nothing reads is still a credential in a deployment's environment, and
the refusal says to rotate it.

**Check at boot what approval depends on.** Provisioning joins the outbound dependencies
already validated at startup — the URL, the client secret, and the realm agreement.

**Derive the whole address from the issuer.** A client-credentials token administers the
realm that minted it and no other, so `DATASPACE_KEYCLOAK_REALM` defaults to the realm
`OIDC_BASE_URL` names. Keycloak also compares a token's `iss` against the address the
request arrived on and answers 401 when they differ — before any role is consulted — so
`DATASPACE_KEYCLOAK_BASE_URL` defaults to the issuer's origin. Both were observed against
a running realm, not assumed: a valid token presented at a second hostname for the same
Keycloak was refused 401, and the same token at the issuer's own hostname was refused 403
for want of roles.

**Separate a misconfiguration from a failure.** `ConfigurationError` deliberately does not
inherit `ValueError`, which the admin API renders into a 422 carrying its message. An
operator is told that the deployment is not configured and who can fix it; the settings,
and Keycloak's own response bodies, go to the server log.

## Consequences

**A manual step in Keycloak, for now.** `../celine-policies` can declare a client's
scopes, audiences and whether it has a service account — it has no field for that service
account's realm roles, and `assign_realm_management_roles` is hard-wired to its own admin
client. Until that repository grows one, the two roles are assigned by hand, and a realm
rebuilt from `clients.yaml` alone comes back without them. The symptom is legible: every
Admin API call refused, `Keycloak user lookup failed (403)`.

**The credential is `svc-ds-onboarding`, which the dataspace owns.**
`clients.ds-host.yaml` adds celine's grants to clients the dataspace declares. Realm
administration of *celine's* realm is not naturally a grant on a dataspace's client;
`svc-onboarding` — celine's own — is the better home, and its declaration already
anticipates the merge. Reusing the credential the service already loads avoided
introducing a second secret to reach the same realm. Whoever consolidates the two clients
should move these roles with them.

**One realm, and a boot refusal for anyone who assumed two.** A deployment provisioning
into a realm other than its issuer's now fails to start rather than failing at the first
approval. That is the intent, and it is a breaking change for a configuration that could
never have worked.

**What this does not fix.** `DATASPACE_KEYCLOAK_DEFAULT_PASSWORD` still sets one shared
initial password on every user this service creates. It is a separate decision and it is
still open.
