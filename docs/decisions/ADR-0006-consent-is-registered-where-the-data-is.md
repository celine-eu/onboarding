# ADR-0006 — A consent is registered at the connector that holds the data, as the community

**Date:** 2026-09-18
**Status:** accepted

## Context

A member of an energy community consents once, here, to a handful of sharing offers.
Until now every one of those decisions was written to a single connector — the
community's — with this service's own `svc-ds-onboarding` token, and an offer whose
data belonged to somebody else was recorded with the member's own credential on the
same connector.

Two things made that wrong, and the second broke it outright.

- **A consent is enforced where the data is served.** The community's connector gates
  the community's datasets. A member's meter readings are held by the grid operator and
  served by the grid operator's data plane, which reads the grid operator's consent
  registry and nothing else. A decision to release them recorded on the community's
  connector enforces nothing at all — and looks, from every side, exactly like one that
  does.
- **A service client speaks for no organisation.** The connector decides what a caller
  may do from the organisation its token names. `svc-ds-onboarding` names none, so a
  holder of it could register a consent at *any* connector for *anybody's* members. The
  connector now refuses it: `POST /consent/admin/shares` answers `403` naming the client
  to use.

The member cannot bridge this themselves. Their credential is linked to their own
community's participant, so the holder's connector refuses it; and `/consent/my/*`
refuses an organisation token in the other direction, by design — a person's consents
are not a service's to read.

## Decision

**Each offer's decision goes to the connector that holds the data it reaches, and this
service registers it as the community, not as itself.**

1. **Routing is configuration, per REC.** The manifest's `dataspace.connectors` names a
   holder, its connector URL, and the offers it holds. An offer named by no entry stays
   at `DS_CONNECTOR_URL`. It is never inferred from the offer: `recipients.recipient`
   says who the data goes *to*, which is not who holds it — a release offer's recipient
   is the community, and the rows are at the grid operator.
2. **The identity is `svc-ds-connector-<alias>`**, the community's own client, with the
   alias taken from `dataspace.organization` and the secret from `DS_ORG_CLIENT_SECRET`.
   The community is the *collector*: the member's relationship is with it. A holder
   admits the write only because it has recorded this community as an accepted consent
   collector.
3. **Whose decision it is is stated.** `subject` where this service relays one the member
   took — the wizard's form, or their own toggle afterwards — and `collector` where the
   community decided itself, which is what a withdrawal on revoked membership is. The
   distinction is not bookkeeping: a relayed withdrawal is the member's, and nothing
   else can lift it; a collector's withdrawal the collector can lift again, which is
   what lets a rejoining member be re-provisioned.
4. **The member's supply points travel with a registration at a holder**, as typed keys.
   That data plane keys its rows by supply point and knows nothing about this community's
   members. A member with no recorded supply point is **refused** rather than registered,
   because a consent that can never yield a row is worse than a visible failure.
5. **The read is merged and fails closed.** The member's own decisions come from their
   own connector as themselves; a holder's come from `GET /consent/admin/subject-shares`,
   per subject. A holder that cannot be reached fails the page rather than rendering a
   granted decision as ungranted.
6. **Prerequisites are presented, not enforced.** `requires_offers` is the connector's
   rule and ds applies it; this service shows what a decision is waiting for.

## Consequences

- **A consent now depends on a credential this service did not need before.** Without
  `DS_ORG_CLIENT_SECRET` nothing is registered anywhere — not even at the community's own
  connector — and the failure names the client. That is deliberate: the alternative is a
  fallback to a token the connector refuses, which would fail two hops away from the
  cause.
- **The member's supply points leave this service.** They are personal data, so they go
  only where they are needed: to a holder, with a grant, and never to the community's own
  connector, never into an evidence record, never into a log. The read-back drops them
  before the page sees them.
- **A decision is in two places, and the member's page is the only thing that knows it.**
  Nothing reconciles them: each connector is authoritative for what it serves. A holder
  that has silently lost a row would show as ungranted, which the member can act on.
- **The member-credential route across participants is gone.** It never worked at a
  holder, and keeping it as a fallback would mean two ways of recording one decision with
  different evidence.
- **Multi-community deployments are limited to one organisation secret.** The client id
  is derived per REC, but the secret is one value for the process, and a manifest is not
  a secret store. A deployment serving two dataspace communities runs an instance per
  community until that is asked for.
- **What will tempt someone to undo it**: routing looks derivable from the offer. It is
  not — the rename of `controller` to `recipient` happened precisely because that field
  was carrying three meanings, and "who holds the data" was never one of them.
