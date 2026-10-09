# ADR-0014 — Registry sync sets up the community's organization through the provisioning reconcile

**Date:** 2026-09-27
**Status:** accepted; amended by [ADR-0017](ADR-0017-the-platform-operators-client-may-start-a-registry-sync.md)

Supersedes the consequence of
[ADR-0004](ADR-0004-ask-the-provisioning-service-instead-of-administering-the-realm.md) that
`provisioning.reconcile` is "a scope this service does not hold and should not". ADR-0004's
decision, and [ADR-0005](ADR-0005-onboarding-is-the-one-caller-of-the-provisioning-service.md)'s
rule that every call to the provisioning service goes through this service, stand.

## Context

A community goes live clean: a registry community, a Keycloak organization with its roles
and groups, and no members
([ADR-0011](ADR-0011-on-a-deployed-realm-every-member-enters-through-onboarding.md)). The
organization has to exist before a platform admin can place the managers in it, and before
the first approval files a participant into it.

The provisioning service's `POST /reconcile/{community}` creates it: it reads the registry
community, ensures the organization, its roles and groups, and provisions an account for each
active member, of which a clean community has none. Its other writer of organizations,
`sync-users` from a YAML file, is local-development only under ADR-0011, and with no members
it stops before it creates one.

Nobody holds `provisioning.reconcile`. ADR-0004 said this service should not, because
sweeping a community and provisioning the one member somebody asked about are different
grants with different owners. Giving the scope to any other client would break ADR-0005:
this service would no longer be the provisioning service's only caller.

## Decision

**This service calls the reconcile, as the first step of a registry sync that is not a dry
run.** `POST /api/admin/recs/{rec_slug}/registry-sync`
([ADR-0012](ADR-0012-areas-are-primary-substation-boundaries-owned-by-the-template.md)) calls
`POST /reconcile/{community}` for the REC's registry community before it writes any area. The
step is the community's set-up: the same realm admin who pushes the areas creates the
organization, in one act.

**A failed or unconfigured reconcile does not stop the area writes.** The areas are the
template's, and writing them does not depend on the organization existing. The sync writes
them anyway, and its answer reports the set-up step as `failed`, with the reason, or
`skipped` when no provisioning service is configured. A re-run completes it: the reconcile
creates only what is missing and the area writes are idempotent.

**This service's client gains `provisioning.reconcile`, as an optional scope** requested only
for the reconcile call, so its default token carries no sweep. It stays the only holder of any
`provisioning.*` scope.

**The registry community is not created here.** When it does not exist the reconcile answers
`404 community_not_found`, and the sync reports it.

The requirement is [REQ-0014](../specifications/registry-sync.md), planned.

## Consequences

- **ADR-0004's worry is answered by who can press, not by what the client holds.** The
  reconcile runs only inside a sync, which only a realm `admins` may start (`recs.write`). No
  unattended job and no organization-level operator reaches it. (Update, 2026-10-03: the
  sync is now started by the realm role `platform-admin` only, and a realm `admins` group
  grants nothing; REQ-0030.)
- **The reconcile provisions every active member it finds.** On a community set up clean that
  is nobody, and later every member already has the account approval gave them. On a
  community seeded from a bundle, a sync creates accounts for its members — which is what the
  reconcile is for, and a local-stack case under ADR-0011.
- **The reconcile's divergence check becomes visible here.** It fails loudly when a member it
  claims to have provisioned is not in the organization; the sync reports that failure rather
  than hiding it, and still writes the areas.
- **A sync can succeed with the community not set up.** The answer says so, per step; an admin
  who reads only the status code misses it. The alternative — refusing the areas until the
  provisioning service answers — would couple two independent writes and make a local stack
  without a provisioning service unable to sync at all.
- **Still one caller of the provisioning service**, so the rules about who may cause an
  account or an email to exist stay in one place.
- **What will tempt someone to undo it:** a scheduled reconcile "to keep organizations in
  shape". That is a second, unattended caller, and a job holding a scope that creates
  accounts. It belongs, if ever, in the provisioning service's own deployment, decided there.
