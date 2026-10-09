# ADR-0017 — The platform operator's client may start a registry sync

**Date:** 2026-10-09
**Status:** accepted

Amends [ADR-0012](ADR-0012-areas-are-primary-substation-boundaries-owned-by-the-template.md)
("held … by no service account", "no unattended sync"),
[ADR-0014](ADR-0014-registry-sync-sets-up-the-community-through-the-provisioning-reconcile.md)
(who can press) and
[ADR-0016](ADR-0016-the-platform-level-is-a-realm-role-not-a-realm-group.md)
(`platform_only_actions` granted "by the role alone"). Their other decisions stand.

## Context

`recs.write`, the registry sync, was reachable only by a person holding the realm role
`platform-admin`. `onboarding-cli registry-sync` refused to start without that person's own
access token (`--token`) or `--local`, and the CLI's client-credentials identity was refused on
the route.

In practice that is not operable. A sync is run from a shell on the deployment, inside the
service's pod, as part of bringing a community up. Getting a person's short-lived access
token into that shell means a browser login, a copy and a paste, for every run; `--local`
skips authorization altogether and is meant for a deployment with no Keycloak.

The platform already has a client for this kind of act: `celine-cli`, the platform operator's
client in celine-policies `clients.yaml`. It holds every service's `.admin` scope,
`onboarding.admin` included, and its secret is the operator's.

## Decision

**A sync is a platform operator's decision, made as a `platform-admin` person or through the
operator's client `celine-cli`.**

- **`recs.write` gains a scope, `onboarding.recs.write`**, in `required_scopes`.
  `onboarding.admin` covers it through the shared matcher's admin override, so `celine-cli`
  reaches the sync without a change to its client. A narrower client can hold
  `onboarding.recs.write` alone once the realm declares it.
- **It stays platform-level for people.** `recs.write` remains in `platform_only_actions`: no
  organization group reaches it, `admins` included, and no realm group does. The person's path,
  the role `platform-admin`, is unchanged.
- **It is not delegated.** A service presenting an operator's token gains nothing for the
  sync: its own scope decides.
- **`onboarding-cli registry-sync` authenticates like every other command.** Without `--token`
  and without `--local` it uses its client-credentials identity (`ONBOARDING_CLI_CLIENT_ID`).
  `--token` with a platform admin's own token, and `--local` under the break-glass rules, work
  as before.
- **`recs.drift` stays people-only.** No command or service reads the drift check, and the
  sync's `--dry-run` already shows the operator what a sync would do.

The policy names a scope, not a client. Every service is authorised by scope here, and pinning
one `azp` would be a second mechanism for one action.

## Consequences

- **Every client holding `onboarding.admin` can sync.** Today that is `celine-cli` and
  `svc-onboarding-cli`, the CLI's default client, which holds the superset as the break-glass
  and end-to-end driver. Whoever holds either secret can rewrite a community's registry areas
  and set up its organization. Keep `onboarding.admin` to operator clients.
- **An unattended sync is now possible.** A scheduled job holding the scope could run one.
  ADR-0012's "no unattended sync" was a consequence of no service holding `recs.write`; it is
  now a matter of who is given the scope. ADR-0014's warning stands: a scheduled reconcile
  "to keep organizations in shape" is a decision for the provisioning service's deployment,
  not a side effect of holding this scope.
- **The audit says which client it was.** A sync started by a client is recorded with
  `actor_type=service` and the client id from the token's `azp`, so a sync from `celine-cli`
  can be told apart from one by a person or from `--local`.
- **Nothing downstream changes.** The provisioning reconcile and the registry writes already
  use this service's own token, whoever started the sync.
