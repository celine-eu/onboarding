# Decisions

Architecture decision records: **why a technical choice was made here**, when the reason
is not derivable from the code and would otherwise be re-litigated.

One file per decision, named `ADR-####-short-slug.md`, with this shape:

```markdown
# ADR-0001 — <the decision, as a statement>

**Date:** <ISO-8601>
**Status:** accepted | superseded by ADR-####

## Context
<what forced a choice. The constraint, and what had already been tried.>

## Decision
<what was decided, in the imperative.>

## Consequences
<what this costs, what it forecloses, and what will tempt someone to undo it.>
```

## What is not an ADR

- **A requirement.** What the product must do belongs with the requirements, where it can
  be traced to a test. An ADR is measured by nothing.
- **A rule with a referent that something already measures.** If a statement could carry
  an identifier and a test that names it, put it where that measurement happens. Deciding
  it here hides it from the report.
- **A procedure.** That is a playbook, and playbooks live in the companion.
- **A fact about the code.** That is knowledge, and knowledge lives in the companion.

An ADR is immutable once accepted. It is superseded by a later ADR that names it, never
edited to say something else.

## The records

| ADR | Decision |
|---|---|
| [0001](ADR-0001-provision-logins-as-the-service.md) | Participant logins are provisioned by this service's own service account, never by a Keycloak administrator |
| [0002](ADR-0002-administer-the-realm-as-celines-own-client.md) | The realm is administered by celine's own client, not the dataspace's — supersedes 0001 |
| [0003](ADR-0003-provision-into-the-group-this-service-may-administer.md) | Participants are created into the one group this service may administer, and found by scanning it — completes 0002 |
| [0004](ADR-0004-ask-the-provisioning-service-instead-of-administering-the-realm.md) | This service holds no Keycloak grant and asks celine-policies' provisioning service for a login — supersedes 0003 |
| [0005](ADR-0005-onboarding-is-the-one-caller-of-the-provisioning-service.md) | Every send to the provisioning service goes through this service, by member-keyed routes a service calls on a manager's behalf — builds on 0004 |
| [0006](ADR-0006-consent-is-registered-where-the-data-is.md) | A member's decision is registered at the connector that holds the data it reaches, under the community's own organisation client |
| [0007](ADR-0007-an-offer-is-recorded-at-every-connector-that-holds-its-data.md) | An offer whose data sits in several places is recorded, and withdrawn, at every connector holding it; a retry writes only what is missing — builds on 0006 |
| [0008](ADR-0008-an-offers-audience-is-read-from-every-connector-that-holds-it.md) | An offer's audience is read from every connector that holds it, checked per dataset, and exported as one offer-scoped file only where every dataset agrees — builds on 0007 |
| [0009](ADR-0009-a-withdrawal-is-reported-in-its-own-column.md) | A member who withdrew is read from every holder's decisions list, as the community, and reported in the export's own withdrawn column — never as an absence, never as an authorisation — builds on 0008 |
| [0010](ADR-0010-the-supply-point-list-is-evidence-not-a-disclosure.md) | The supply-point export is the collector's own dated evidence: it records no `DataDisclosed`, posts nothing to any connector and names no recipient — supersedes 0008 decision 5 and 0009's disclosure consequence |
| [0011](ADR-0011-on-a-deployed-realm-every-member-enters-through-onboarding.md) | On a deployed realm every member enters through this service, into a community that starts clean with its managers first; the YAML bundle and `sync-users` are local-development tools |
| [0012](ADR-0012-areas-are-primary-substation-boundaries-owned-by-the-template.md) | A community's areas are GSE primary-substation boundaries, declared in its template, validated against the Digital Twin at import, and pushed to the registry only by an explicit, additive, platform-admin sync |
| [0013](ADR-0013-eligibility-and-area-are-decided-by-boundary-through-the-digital-twin.md) | For a template with boundaries, the boundary containing the supply address decides eligibility and the area, through the Digital Twin; it fails closed, the server resolves and keeps only the boundary id, and the rate-limited anonymous answer is yes or no — builds on 0012 |
| [0014](ADR-0014-registry-sync-sets-up-the-community-through-the-provisioning-reconcile.md) | A registry sync first sets up the community's organization through the provisioning reconcile, which does not stop the area writes when it fails, and this service gains `provisioning.reconcile` as an optional scope — supersedes 0004's reconcile consequence |
| [0015](ADR-0015-a-correction-is-a-revision.md) | From submission on, a POD, name or email changes only through an append-only, operator-validated revision; after approval it is propagated step by step to every system holding a copy, each step recorded and retryable, never with a value |
| [0016](ADR-0016-the-platform-level-is-a-realm-role-not-a-realm-group.md) | The only platform-wide grant is the realm role `platform-admin`; an organization's groups grant only inside that organization, and a realm group grants nothing — narrows the realm-level grant 0003, 0012 and 0014 rely on |
