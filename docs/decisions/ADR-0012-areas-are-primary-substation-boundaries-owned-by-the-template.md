# ADR-0012 — A community's areas are primary-substation boundaries, declared in its template and pushed to the registry on request

**Date:** 2026-09-27
**Status:** accepted; amended by [ADR-0017](ADR-0017-the-platform-operators-client-may-start-a-registry-sync.md)

## Context

A registry member names an area; the area lists topology nodes; the pipelines take the
area's first topology node as the member's primary substation and compute shared energy per
substation. The GSE incentive is defined per primary substation (*cabina primaria*). The
area exists to model that incentive, and nothing else on the platform depends on it.

Today a template maps each area to a list of municipalities, with a required
`default_area` for everyone else, and [templates.md](../templates.md) called that "the
design, not a stand-in for geoshapes". It is a stand-in. A municipality can straddle two
substations, the registry accepts an area with any number of nodes and two areas on one
substation, and nothing checks that the node the pipelines pick is the one the member's
supply point sits in.

The GSE publishes the substation areas, and the platform already holds them: a gold table
with one row and one shape per `cod_ac` (the substation code), exposed through dataset-api
and readable through the Digital Twin's value fetchers. Registry areas were written by bundle
import only, and nothing wrote them from here.

## Decision

**An area is a reference to one GSE primary-substation boundary.** A template declares it
as `rec_registry.areas.<area_key>.boundary: {source: gse_cabine_primarie, id: <cod_ac>}`.
The area key stays a hand-managed, readable name; the reference maps it to exactly one
substation. Keys equal to the `cod_ac` were rejected: unreadable, and tied to one country's
code scheme. `source` is a closed set so that another country's boundaries are a new value,
not new code.

**The template is the source of truth for the community's areas, and admins author it.**
No shape is copied into the template or the registry. The shapes stay in gold, which follows
the GSE's revisions.

**Template import validates every boundary against the Digital Twin.** An id is known when
the `boundary_shape` fetcher answers a shape for it; an unknown id, two areas on one id, or a
Digital Twin that cannot be reached refuses the template. This service holds no geometry and
no list of codes of its own.

**A template with boundaries has one eligibility answer, and asks it.** Import also refuses
such a template when it declares `coverage.rules` beside the boundaries — a second rule set
the boundary path would ignore while it reads as applying — and when its wizard steps omit
`eligibility`, the step where the supply address meets the boundaries
([ADR-0013](ADR-0013-eligibility-and-area-are-decided-by-boundary-through-the-digital-twin.md)).

**The template's areas reach the registry only through an explicit sync.**
`POST /api/admin/recs/{rec_slug}/registry-sync`, and an `onboarding-cli` command on the same
route, with `dry_run` and `prune`. It writes each area with its boundary and one topology id,
and one `primary_substation` topology node with that id. It is additive: an area the
template no longer declares is deleted only with `prune=true`, and the registry refuses while
members reference it. It never runs on a template load or reload: templates reload on a
short cache, and an unattended registry write every time a file changes is not an
admin-driven act.

**The sync needs a new capability, `recs.write`, held by realm-level `admins` only, and by
no service account.** (Update, 2026-10-03: the platform level is now the realm role
`platform-admin`, not the realm group `admins`, which grants nothing; REQ-0030.) It lives beside the manifest reload under `/api/admin/recs`, and it
writes a whole community's registry data. No scope grants it, `onboarding.admin` included, so
a sync always follows a person's decision. The `onboarding-cli` command therefore
authenticates with a realm admin's own token (`--token`), or runs `--local` in process under
the break-glass rules; the CLI's client-credentials identity cannot start a sync.

**This service gains two registry scopes.** `rec-registry.community.write` for the writes,
**optional** and requested only for the call that writes, so this service's default token
carries no community write. `rec-registry.read` for reading back the community's areas,
topology and member counts, which the dry run, the prune count and the console's drift check
need: the registry's community `GET` routes answer only to `rec-registry.read` (or
`.admin`), and this service holds neither today. No new scope is introduced for the reads.

**Municipality lists and `default_area` remain only for a template that declares no
boundaries.** Such a template keeps working as today.

The requirements are
[REQ-0001, REQ-0002](../specifications/eligibility-and-areas.md), implemented (an area
key matches `^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`), and
[REQ-0009 to REQ-0015](../specifications/registry-sync.md), planned: the registry sync is
not built yet.

## Consequences

- **One area is one substation.** The pipelines' "first topology node" is the substation
  because it is the only one. The registry enforces the same shape on its side — one
  boundary, one `primary_substation` node with the same id, no two areas of a community on
  one id — so a bundle import cannot reintroduce the old graph unnoticed. The console's drift
  check shows it if one does.
- **A new runtime dependency at template import.** Importing a template with boundaries
  needs the Digital Twin up. A template without boundaries does not.
- **`rec-registry.community.write` is registry-wide**, as every registry grant is. This
  service only syncs the communities its templates bind, only when a realm admin asks, and
  only holds the scope in the token of that call.
- **`rec-registry.read` is registry-wide too**: this service can read every community's
  members, as the dashboard's backend already can. It needs the areas and topology of the
  communities its templates bind; the reads are not narrower than the scope.
- **No unattended sync.** With no service account able to hold `recs.write`, a scheduled job
  cannot run one; a person's token or shell access is always behind it.
- **A member's stored area does not move when a boundary does.** GSE revises its
  perimeters and a reload of gold picks that up; a member already registered stays where
  they are until a manager moves them.
- **Removing an area is two steps on purpose.** Move its members (a manager, on the
  dashboard), then prune. The sync answers the member count so the first step is not a
  guess.
- **The GSE shapes are conventional areas.** The authoritative supply-point-to-substation
  assignment is the distributor's. A boundary is the best evidence the platform has until
  that exchange exists.
- **What will tempt someone to undo it:** a community whose template is not ready, and an
  admin who could "just" edit areas in the registry. An area written there directly is one
  the next sync reports as undeclared, and one a later prune removes.
