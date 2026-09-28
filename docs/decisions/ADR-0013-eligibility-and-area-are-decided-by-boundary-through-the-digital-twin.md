# ADR-0013 — Eligibility and the member's area are decided by boundary, through the Digital Twin, and only the boundary id is kept

**Date:** 2026-09-27
**Status:** accepted

Builds on [ADR-0012](ADR-0012-areas-are-primary-substation-boundaries-owned-by-the-template.md),
which made a community's areas primary-substation boundaries.

## Context

Two decisions about a new member are made from their supply address. **Eligibility** comes
from the manifest's `coverage.rules`, matched against the municipality or postal code the
geocoder returns. **The area** comes from the template's municipality lists, falling back to
`default_area`. The geocoder resolves the address to a point, and the point is then thrown
away; only `supply_municipality` is stored.

The regulatory perimeter of a CER is the primary substation, not the municipality
([regulatory-compliance.md](../regulatory-compliance.md#g1-perimeter-model), G1). That
analysis also recommended persisting the geocoded point (G2). With areas now declared as
boundaries, the question "is this address inside the community, and where" has one precise
answer, and the point is only a means to it.

This service has no geometry and no spatial database. The Digital Twin's value fetchers read
the substation shapes in gold.

## Decision

**For a template with boundaries, one lookup decides both.** The geocoded point goes to the
Digital Twin fetcher `boundary_at_point(source, lat, lon)`, which answers at most one id.
The applicant is eligible only if that id is the boundary of one of the template's areas, and
their area is that area. Outside every declared boundary, not eligible. No fallback to a
municipality, to `coverage.rules` or to a default area.

**An unreachable Digital Twin fails closed and keeps nothing.** The check answers 503, as an
unreachable geocoder does, and the wizard says the address cannot be checked right now. It
never admits by default and never refuses by default.

**The submission keeps the boundary id and its source — `supply_boundary_id`,
`supply_boundary_source` — and never the coordinates.** The id is all later steps need and it
is far less identifying than a point. This supersedes G2's recommendation to persist the
geocoded latitude and longitude.

**The server resolves the id, from the submission's own supply address; the client never
sends one.** The resolution runs when the submission is saved or submitted, and again at
approval. A boundary id that arrived from the browser would be one the applicant chose; the
wizard's copy of an eligibility answer is not evidence of where the supply point is.

**Approval resolves the boundary again**, from the same address, against the template in
force at approval. A boundary that no area declares any more fails the registry step with
`boundary_not_in_community`, rather than falling back to a municipality, a default area or
the id stored at submission.

**The call is this service's, not the applicant's.** The eligibility route stays
anonymous; the Digital Twin is called with this service's own token and scope
`digital-twin.values.read`.

**The anonymous answer is eligible yes or no only**: no boundary id, no area. An anonymous
caller learns whether an address is inside the community, never where inside it.

**The anonymous route is rate-limited here**, per client, answering `429` over the limit,
as the other public routes already are.

**A template without boundaries keeps today's municipality path**, and makes no Digital Twin
call.

**Amended 2026-09-28: the supply address is the one the wizard checked.** The eligibility
step saves the address it geocoded on the submission (`supply_address`, encrypted, in the
shape the geocoder takes), and the server resolves the boundary from it at submit and at
approval, with the scanned `extracted_data.indirizzo` only as the fallback. Until then the
only supply address a submission held was the scanned one, so a community with document
scanning off could not submit at all. The coordinates are still never stored. Also: a
Digital Twin outage during find-by-address leaves out only the boundary communities, the
others still answered.

The requirements are [REQ-0003 to REQ-0008, REQ-0016, REQ-0018 and
REQ-0019](../specifications/eligibility-and-areas.md), all implemented.

## Consequences

- **The Digital Twin is on the wizard's critical path** for a community with boundaries.
  When it is down, nobody in that community can pass the eligibility step. That is the
  intended failure: an admission nobody computed is worse than a wait.
- **The supply point's coordinates leave this service**, to the Digital Twin, on every check,
  and are not kept by it. The processing record has to say so.
- **An anonymous route now causes a credentialed internal call.** Each unauthenticated
  request costs a geocoder call and a Digital Twin call made with this service's identity.
  The per-client rate limit bounds what one caller can spend, and how fast one caller can
  probe the perimeter; a community whose applicants share one NAT address shares one budget,
  which is why the limit is configurable.
- **Resolving at save, submit and approval costs a geocoder and a Digital Twin call each
  time.** That is the price of never trusting a boundary the browser carried. The same
  fail-closed rule applies at each: no answer, no id, no registration.
- **The applicant is not told their area or substation.** They learn they are eligible; the
  reviewing operator sees where.
- **The reviewing operator sees the substation**, and a manager who later moves the member
  to another area leaves the submission's id as the record of where the supply point is.
- **A submission started under one template and approved under another** can fail at step 2
  with `boundary_not_in_community`. That is the point of re-resolving: the member is never
  registered into an area the community no longer has.
- **What will tempt someone to undo it:** a Digital Twin outage during an onboarding drive,
  and a flag to "fall back to municipality for now". That flag admits people outside the
  perimeter and files them under a guessed area, silently, one submission at a time.
