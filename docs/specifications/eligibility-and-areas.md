# Eligibility and areas by primary-substation boundary

Where a new member lands. A template either maps each registry area to a list of
municipalities and decides eligibility from `coverage.rules`
([templates.md](../templates.md#rec-registry-binding-optional-per-community)), or declares
each area as one GSE primary-substation boundary (*cabina primaria*, identified by its
`cod_ac`), and then the boundary that contains the supply address decides both whether the
applicant is eligible and which area they join. The reasons are in
[ADR-0012](../decisions/ADR-0012-areas-are-primary-substation-boundaries-owned-by-the-template.md)
and
[ADR-0013](../decisions/ADR-0013-eligibility-and-area-are-decided-by-boundary-through-the-digital-twin.md).

The shapes are not held here. They are resolved through two Digital Twin value fetchers:
`boundary_at_point(source, lat, lon)`, which answers at most one boundary id, and
`boundary_shape(source, ids)`, which answers a GeoJSON shape for each requested id it knows.

---

### REQ-0001 — a template may declare each area as one primary-substation boundary

A `rec_registry` block may declare its areas as boundary references:

```yaml
rec_registry:
  community: example-rec
  areas:
    north:
      boundary: {source: gse_cabine_primarie, id: AC000E00000}
```

- `source` is one of a closed set of boundary sources. The set starts with one value,
  `gse_cabine_primarie`; any other value is refused at template import.
- `id` is the boundary's identifier in that source — for `gse_cabine_primarie`, the
  primary substation's `cod_ac`.
- A template that declares boundaries declares **no** municipality lists and **no**
  `default_area`: those remain only for a template that declares no boundaries (REQ-0004).
  A template mixing the two is refused at import, as is one declaring `coverage.rules` beside
  boundaries or omitting the `eligibility` step (REQ-0017).

Area keys stay hand-managed, readable names, and each must match
`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$` — letters, digits, `-` and `_`, starting with a letter or
digit, at most 128 characters — because the key goes into the registry's
`PUT …/areas/{area_key}` path; any other key is refused at template import. The boundary
reference is what maps a name to exactly one substation. An area may also give an optional
display `name` beside its `boundary` (REQ-0021).

### REQ-0002 — template import refuses an unknown boundary and two areas on one boundary

`task import-templates` validates every boundary a template declares before the template is
stored:

- **An unknown id is refused.** An id is known when the Digital Twin's `boundary_shape`
  answers a shape for it. This service holds no geometry and no list of codes of its own.
- **Two areas on the same boundary are refused.** Two names for one substation are
  indistinguishable to everything downstream.
- **A boundary that cannot be checked is not accepted.** When the Digital Twin cannot be
  reached, the template is refused with that reason rather than imported unvalidated.

A refused template is not imported, so the wizard never runs on a template that could not be
synced to the registry (REQ-0013).

### REQ-0003 — for a template with boundaries, the boundary decides eligibility and the area

The eligibility step geocodes the supply address to a point, as it does today, and asks
`boundary_at_point` for the template's boundary source.

- **Eligible only if** the answered id is the boundary of one of the template's areas.
- **The member's area is that area.** Nothing else chooses it.
- **Outside every declared boundary is not eligible**, including a point inside a boundary
  the template does not declare, and a point inside none.
- **No fallback** to a municipality match, to `coverage.rules`, or to a default area.

### REQ-0004 — a template without boundaries keeps the municipality path

A template whose `rec_registry` areas are municipality lists, or which declares no
`rec_registry` block, behaves as today: eligibility from `coverage.rules`, the area from the
municipality lists with `default_area` as the fallback. No Digital Twin call is made for it.

### REQ-0005 — an unreachable Digital Twin fails closed and keeps nothing

When `boundary_at_point` cannot be answered — the Digital Twin is unreachable, times out, or
refuses the service's token — the eligibility check answers **503**, the same answer an
unreachable geocoder gives today, and the wizard tells the applicant the address cannot be
checked right now.

- It never answers `eligible: true`, and never `eligible: false`: neither was computed.
- Nothing from the attempt is written onto the submission.

### REQ-0006 — the anonymous eligibility answer is eligible yes or no, and nothing about where

`POST /api/{rec}/eligibility` needs no credential from the caller. For a template with
boundaries it now makes a call to the Digital Twin with **this service's own** token (scope
`digital-twin.values.read`); the caller's request never carries or receives that token.

The eligibility outcome is **yes or no only**. The answer carries **no boundary id and no
area key**, and no matched rule or matched value that would name either. An anonymous caller
learns whether an address is inside the community, never which substation or area it falls
in. The route is rate-limited (REQ-0016).

**The answer carries no coordinate either**: no `lat` or `lng`, for any community, whether the
caller sent an address or a point. The point the geocoder returns for an address is used to
decide and then dropped (REQ-0007); an anonymous route that handed it back would turn this
service into a free geocoder, and the point is more precise than anything the wizard shows.
The answer keeps the address fragments the wizard displays (municipality, postal code, state,
country code) and the refusal's reason.

### REQ-0007 — the server resolves the submission's boundary from its own supply address, never from the client

For a submission of a template with boundaries, `supply_boundary_id` and
`supply_boundary_source` are **resolved by this service** from the supply address the
submission itself holds: geocoded, then asked of `boundary_at_point`. The resolution runs
**when the submission is saved or submitted**, and again at approval (REQ-0008). The supply
address a submission holds is the one the eligibility step checked (`supply_address`,
REQ-0018) or, when there is none, the scanned `extracted_data.indirizzo`; a submission with
neither has nothing to resolve, and cannot be submitted.

- **The client never sends a boundary id.** No request schema declares `supply_boundary_id`
  or `supply_boundary_source`, so a value a client puts in a save or submit is never stored.
  The anonymous eligibility answer carries no id to send back (REQ-0006), and nothing the
  wizard's browser holds decides the boundary.
- **The geocoded coordinates are not stored**, on the submission or anywhere else. The id is
  all later steps need, and it is far less identifying than a point.
- A resolution that cannot be computed — geocoder or Digital Twin unreachable — records no
  id and never a guessed one (REQ-0005); a submission with a supply address outside every
  declared boundary cannot be submitted, as it cannot pass the eligibility step.
- A later save that changes the supply address resolves again; the stored id is always the
  one the current address resolves to.
- The reviewing operator sees the recorded id beside the area it resolves to, named by its
  display name (REQ-0023).

### REQ-0023 — the review shows the substation and the area by its display name

The console's submission review shows a resolved boundary as "primary substation `<id>`, area
`<name>`", where `<name>` is the **display name** the template in force gives that area
(REQ-0021), or its key when the template gives none. The admin read of a submission carries
both: `supply_boundary_area` (the key) and `supply_boundary_area_name` (the display name), and
neither when the template in force has no area on that boundary. The applicant's own read
carries neither (REQ-0006).

### REQ-0008 — approval resolves the boundary again and refuses one no longer in the template

Step 2 of approval (the registry member) **resolves the boundary again** from the
submission's supply address, as at save and submit (REQ-0007), and maps it to an area of the
template **in force at approval**, not the one in force when the wizard ran. The id it
resolves is the one recorded on the submission and the area it maps to is the one the member
is registered into.

- When no area of that template has that boundary, the step fails with
  `boundary_not_in_community` and registers nothing. It does not fall back to a municipality,
  a default area, or the id stored at submission.
- When the resolution cannot be computed (geocoder or Digital Twin unreachable), the step
  fails, retryable, and registers nothing.
- Retrying the step resolves again, so it succeeds once the template, and the registry synced
  from it, declare that boundary again.

### REQ-0016 — the anonymous eligibility route is rate-limited per client

`POST /api/{rec}/eligibility` is rate-limited in this service, **per client** (the caller's
address, as the other public routes are keyed), with a configurable limit beside the
existing public-endpoint limits (`RATE_LIMIT_ELIGIBILITY`, default `30/hour`). A caller over the limit is answered **429** and causes no
geocoder call and no Digital Twin call.

The limit is per real client only when uvicorn trusts the ingress's forwarded headers
(`FORWARDED_ALLOW_IPS` set to the ingress's range, see the README's security section);
without it every visitor is keyed by the ingress's address and shares one limit.

Each anonymous check costs a geocoder call and, for a template with boundaries, a Digital
Twin call made with this service's identity (REQ-0006); without a limit the route lets
anyone spend both, and probe the community's perimeter address by address.

### REQ-0017 — template import refuses `coverage.rules` beside boundaries, and a boundary template without an eligibility step after `consents`

`task import-templates` refuses, with the reason, a template that declares its areas as
boundaries (REQ-0001) and also:

- **declares `coverage.rules`.** For such a template the boundary alone decides eligibility
  (REQ-0003); rules beside it would be a second, ignored answer that reads as if it applied.
- **omits the `eligibility` step from its wizard steps.** The eligibility step is where the
  supply address is checked against the boundaries; without it an applicant outside every
  boundary could reach submission.
- **places the `eligibility` step before the `consents` step, or has no `consents` step.**
  The address the eligibility step checks is saved on the submission (REQ-0018), and the
  submission exists only once `consents` has created it; checked before that, the address
  is lost, and the submission has nothing to resolve its boundary from at submit and at
  approval.

The registry sync applies the same checks before it writes (REQ-0013).

### REQ-0018 — the wizard saves the supply address it checked, and the boundary is resolved from it

The eligibility step saves the address it checked on the submission as `supply_address`, in
the shape the geocoder takes: `{"text": "<address>"}`, the free-text query exactly as it was
geocoded (1 to 300 characters, surrounding spaces trimmed; any other key is refused, so no
point, boundary or area the browser computed rides along with it).

- **It is the supply address REQ-0007 and REQ-0008 resolve from**, at save, at submit and at
  approval. The scanned `extracted_data.indirizzo` is the fallback, used only when no checked
  address is saved.
- **So a community whose areas are boundaries needs no document scanning**: with scanning off,
  a submission holding only the checked address can be submitted and approved into the area
  of the boundary it resolves to.
- **Stored encrypted**, like every other fragment of the participant's address, and **never
  logged**. The coordinates the geocoder returns for it are still not stored (REQ-0007).
- The applicant's own read of the submission returns it; it never returns the boundary.

### REQ-0019 — find-by-address is rate-limited, and a Digital Twin outage leaves out only the boundary communities

`POST /api/recs/find-by-address`, the eligibility check across every community, is
anonymous and shares the eligibility route's per-client limit (`RATE_LIMIT_ELIGIBILITY`,
REQ-0016): over it the caller is answered **429** and causes no geocoder or Digital Twin
call.

When the Digital Twin cannot answer for a community whose areas are boundaries, **that
community fails closed on its own**: it is left out of the list, never listed on a guess. The
communities that do not depend on the Digital Twin (municipality lists, `coverage.rules`, no
coverage at all) are still answered, and the route answers **200**. The log names the community
left out, never the address or the point.

The answer is `{"matches": [<community>, …], "unchecked": <bool>}`, and like the eligibility
answer (REQ-0006) it carries **no coordinate**: no `lat` or `lng`, at the top level or in a
match. **`unchecked` is true when
any community was left out because it could not be checked**, so the page says to try again
later rather than that no community covers the address — which is not what was found. It
names no community: the flag says only that the list may be incomplete.
