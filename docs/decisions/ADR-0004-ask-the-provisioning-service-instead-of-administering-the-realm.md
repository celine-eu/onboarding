# ADR-0004 — This service asks for a login instead of administering the realm

**Date:** 2026-09-12
**Status:** accepted

Supersedes [ADR-0003](ADR-0003-provision-into-the-group-this-service-may-administer.md),
and with it the whole line of reasoning in
[ADR-0001](ADR-0001-provision-logins-as-the-service.md) and
[ADR-0002](ADR-0002-administer-the-realm-as-celines-own-client.md): each of those chose a
*narrower* Keycloak credential for this service to hold. This one takes the credential
away.

## Context

ADR-0001 replaced a realm administrator's password with this service's own service
account. ADR-0002 chose celine's client over the dataspace's. ADR-0003 scoped it to a
fine-grained admin permission over one realm group — the narrowest grant Keycloak can
express — and measured what that costs: no user search, so a `409` from `POST /users`
triggers a paged scan of the group; a duplicate *outside* the group that this service can
neither read, adopt, nor move, and which therefore fails the enablement step with an
instruction for a human.

Two facts ended the line.

**The narrow grant could not finish the job.** A participant's community membership in
Keycloak is an **organization**, and the `organization` claim is what every org-scoped
policy on this platform resolves them by. Organization membership is the Organizations
API, which no fine-grained admin permission reaches. So a participant provisioned from
here landed in `/participants` and in no organization: authorised for nothing, with
nothing saying so. On the development realm, 10 of 45 members were in that state.

**A public front door held admin rights over accounts.** That is true of any grant
narrow enough to still create a user, and narrowing it further does not change the shape
of the risk — only its blast radius.

`../celine-policies` now runs `celine.provisioning`: one stateless service, the only
writer of participant accounts, holding realm-wide `manage-users` + `manage-realm` and
safe to hold them because it has **no public route** (celine-policies ADR-0007). Its
`clients.yaml` has already removed `svc-onboarding`'s `admin_permissions` block and
granted it the scope `provisioning.participants.write` instead — granted ahead of this
change deliberately, so the cutover is a code change here rather than a realm change that
has to land in the same breath.

## Decision

**Call the provisioning service. Hold no Keycloak grant.**
`services/keycloak_identity.py` is deleted rather than kept behind a flag: a fallback path
to the Admin API is a grant somebody has to keep granting, and a test asserts that no
source file here names `/admin/realms/`. Two calls remain, through
`celine.sdk.provisioning` — `PUT /participants/{community}/{key}` on approval and
`POST …/disable` on revocation.

**`(community, key)` is `rec_registry.community` and `submission.ref`** — the same pair
this service already writes the registry member under. One alias from one place, because
the provisioning service files the account into that community's Keycloak organization and
the sweep that later checks the filing reads the community's own id from the registry
export.

**A REC that declares no `rec_registry` block gets no login, and the step says so.**
This is a narrowing, and it is deliberate. Revocation resolves `(community, key)` through
the registry export, so a login provisioned for such a REC could never be revoked through
this seam; and the community alias would have to be invented, creating a Keycloak
organization that no reconcile ever looks at. An unrevokable login is worse than an absent
one.

**`PROVISIONING_URL` replaces `DATASPACE_KEYCLOAK_ENABLED`, `_BASE_URL`,
`_PARTICIPANTS_GROUP` and `_UPDATE_EXISTING`, and startup refuses all four if set.**
Empty disables the login step — the shape `REC_REGISTRY_URL` and `DS_CONNECTOR_URL`
already have here, where an address is the only thing that decides whether a dependency is
there. `DATASPACE_KEYCLOAK_ENABLED=true` is the one that has to be refused rather than
warned about: it reads as "participants are being given logins" and, with nothing reading
it, none would be — silently, one approval at a time. `DATASPACE_KEYCLOAK_REALM` is
**not** removed; it names the realm the account lives in for the dataspace step and
administers nothing.

**Revocation order is no longer the reverse of the provisioning order.** Disabling a login
resolves the member through the registry export, and that export carries only `active`
members — so deactivating the member first makes the disable a `404`, leaving a revoked
participant able to sign in and a step row saying the revocation succeeded. The login is
closed before the member is deactivated, declared as `enablement.REVOKE_ORDER`.

## Consequences

**A participant now lands in their community's Keycloak organization**, which is the
defect that motivated the move. Nothing here can verify it: the `organization` claim is
asserted by the reconcile sweep, which needs `provisioning.reconcile` — a scope this
service does not hold and should not. Sweeping a community and provisioning the one
member somebody asked about are different grants with different owners.

**The `409`-then-scan dance, the group as a permission boundary, the issuer-address 401,
the missing-group-looks-like-a-missing-grant 403, and the unreachable duplicate are all
gone.** The provisioning service has realm-wide reach, so for it there is no outside: an
account created by anything else is found by address and adopted, where this service
previously had to ask a person to intervene. ADR-0003's whole "Consequences" section is
void.

**One more hop on the critical path of an approval**, and one more service that has to be
up for step 1 to succeed. It fails closed as it always did, and `502` from provisioning —
Keycloak having failed rather than a refusal — is the retryable case the step row exists
for.

**This service's outbound Keycloak identity is now a scope, not a grant**, which is why
`service_auth.keycloak_admin_token_provider` was renamed `celine_token_provider`. A name
saying "keycloak admin" would keep describing rights this client does not have, and that
is the kind of name a later reader grants back.

**What will tempt someone to undo it.** The provisioning service having no public route
makes it unreachable from a developer's laptop without the compose stack up, and the
shortest way past that is a small Admin API call "just for local development". That is
exactly the grant this ADR removes, and it would have to be granted in
`../celine-policies` to work at all — which is the check. Run the stack, or leave
`PROVISIONING_URL` unset and onboard without logins.
