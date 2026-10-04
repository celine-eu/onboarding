# Registry sync

How a template's areas reach the REC registry. The template is the source of truth for a
community's areas; this service writes them to the registry when a platform admin asks, and
at no other time. The reasons are in
[ADR-0012](../decisions/ADR-0012-areas-are-primary-substation-boundaries-owned-by-the-template.md)
(the areas) and
[ADR-0014](../decisions/ADR-0014-registry-sync-sets-up-the-community-through-the-provisioning-reconcile.md)
(the set-up step).

---

### REQ-0009 — registry sync is an explicit platform-admin action

`POST /api/admin/recs/{rec_slug}/registry-sync?dry_run=&prune=` pushes one REC's template
areas to the registry community its `rec_registry.community` names. An `onboarding-cli`
command calls the same route.

- **Capability `recs.write`**, granted to the realm role `platform-admin` only (REQ-0030).
  No organization group grants it, `admins` included, and no realm group does: the sync
  writes registry data and provisioning state for a whole community.
- **No service account holds `recs.write`.** No scope grants it, `onboarding.admin`
  included, as none grants `members.invite` alone: a sync always follows a person's decision.
- **The CLI authenticates as that person, or runs in-process.** `onboarding-cli`'s
  registry-sync command takes a platform admin's own token (`--token`), or runs `--local` in
  process, through the same service layer, under the break-glass rules. Its
  `svc-onboarding-cli` client-credentials token is refused on this route.
- **Nothing else runs it.** Loading, reloading or importing a template never writes to the
  registry.
- **An unknown REC is `404`**; a caller without `recs.write` is `403` before anything is
  read or written. A platform admin sees `recs.write` among the REC's capabilities in
  `GET /api/admin/me`; no organization group does.
- **The CLI refuses to start without `--token` or `--local`** (exit code 2), so its
  client-credentials identity is never tried. It exits 1 when any area or node was refused
  or the set-up step (REQ-0014) failed.
- **A real sync is audited** (`registry_sync`, entity `rec`), with counts only: how many
  areas and nodes were created, changed, left alone, deleted or refused, and the set-up
  step's status. A dry run records nothing.
- The registry calls use this service's own token: `rec-registry.community.write` for the
  writes, requested for the call that writes and not carried by this service's default token;
  `rec-registry.read` for reading the community's areas, topology and member counts
  (REQ-0010, REQ-0012, REQ-0015).

### REQ-0010 — a dry run writes nothing and lists the plan

With `dry_run=true` the route writes nothing — not to the registry, not to the provisioning
service — and answers what a real run would do: each area and topology node that would be
created, changed, left alone, or refused, and each area in the registry that the template no
longer declares, with its member count.

- It reads: the registry community (areas and topology) and, for each undeclared area, how
  many members name it, with this service's `rec-registry.read`.
- The answer has the same shape as a real run's, `dry_run: true`, the set-up step
  `not_run`. Each item's `outcome` is one of `created`, `changed`, `unchanged`, `refused`
  (with a `code` and a `reason`), `deleted`, `undeclared` (a registry area the template no
  longer declares, kept without `prune`), `renamed` (a registry area moved to this key,
  with `renamed_from` and `members`; REQ-0011) or `not_run` (the registry stopped answering
  earlier in the run); in a dry run it is what a real run would do. For a renamed area the
  dry run counts the members that would move, with `rec-registry.read`.

### REQ-0011 — the sync is additive and a second run changes nothing

For each area of the template, the sync writes the registry area with its boundary reference
and exactly one topology id, the boundary id; and one topology node with that id and type
`primary_substation`.

- **What is written.** The node `{id: <boundary id>, type: primary_substation, name:
  <area name>}` through `PUT …/topology/{id}`, **before** the area that needs it; then the
  area `{name: <area name>, boundary: {source, id}, topology: [<boundary id>]}` through
  `PUT …/areas/{area_key}`. The area name is the template's display name for the area, or
  its key when it gives none (REQ-0021). What the template does not own is kept when either is written
  again: a node's `operator_id`, `parent` and `area`, an area's `location` and `geometry`.
- **Additive by default.** An area the registry holds and the template does not declare is
  reported, never deleted, unless `prune=true` (REQ-0012).
- **Idempotent.** A run straight after a successful run writes nothing and reports every
  area and node as unchanged.
- **A renamed area is moved, with its members.** When a template area's key is not in the
  registry and its boundary is held there by exactly one area under a key the template no
  longer declares, the sync asks the registry to rename that area:
  `POST …/areas/{old_key}/rename` with `{"new_key": <area key>}`, with
  `rec-registry.community.write`. The registry moves the area as stored, every member whose
  `area` is the old key (whatever their status) and its boundary in one transaction under the
  community lock, and removes the old key. It is one request, made after the nodes and
  before any prune; then, when the area as stored differs from what the template wants (its
  name, say), the area's ordinary `PUT` under the new key. The answer lists it once, under
  the new key: `outcome: renamed`, `renamed_from: <old key>`, `members: <how many moved>`,
  as the registry counts them; the old key is not listed as undeclared, and no `prune` is
  needed, with members in the old area or without. A dry run lists the same, with the
  members counted and nothing moved. A rename the registry refuses is `refused` with the
  registry's code (`area_key_taken`, `area_not_found`, `invalid_area_key`), and the old area
  stays, reported as undeclared.
- **When the new key is already in the registry** on another boundary, nothing is renamed:
  the new key is refused, `code: boundary_held`, naming the old one; a sync with
  `prune=true` deletes the old area first when no member holds it (REQ-0012).
  An area moving to another boundary frees its old one for another area in the same run.
- **A refusal of one write is reported, and the rest goes on.** An area whose node was
  refused is refused too (`topology_node_not_written`); a registry that stops answering
  leaves every later write `not_run`. `ok` is false whenever any item is refused or not
  run.

### REQ-0012 — pruning refuses an area that still has members, and says how many

With `prune=true` the sync also deletes each registry area the template no longer declares.
The registry refuses to delete an area that members still reference; the answer names that
area and its member count, so the admin moves those members first.

- The members are **counted**, page by page, with this service's `rec-registry.read`; none
  is returned, logged or recorded. An area with members is reported `refused`,
  `code: area_in_use`, with `members: <count>`, and is not asked to be deleted; a member
  who moved in after the count is the registry's own `area_in_use`, counted again.
- After a pruned area is gone, the topology node only it listed is deleted too, unless the
  template or another remaining area uses it.

### REQ-0013 — a template that fails validation is not synced

Before writing anything, the sync applies the checks of template import (REQ-0002, REQ-0017)
to the template as it is now: every boundary id is known to the Digital Twin, no two areas
share a boundary, no `coverage.rules` beside boundaries, an `eligibility` step present and
after `consents`, every area key is a valid registry key. A template that fails any of them
is not synced, and the answer says why.

- A template that fails is `422 {"detail": {"code": "template_invalid", "message": …}}`; a
  template with no `rec_registry` block, or whose areas are municipality lists, is `422
  template_not_syncable`; a Digital Twin that cannot be asked is `503
  boundaries_unavailable`. In each case nothing is read from the registry and nothing is
  called after.
- The registry must be configured (`503 registry_not_configured` otherwise) and must hold
  the community (`404 community_not_found`); a registry that cannot be read is `502
  registry_unavailable` (or `registry_refused` for a refused read). These are answered
  before the set-up step and before any write.

### REQ-0014 — a real sync first sets up the community through the provisioning reconcile

A sync that is not a dry run first calls the provisioning service's
`POST /reconcile/{community}` with this service's token, requesting `provisioning.reconcile`
for that call only (it is not in this service's default token).
That call creates the community's Keycloak organization, its roles and its groups, and
leaves what already exists in place. It also provisions an account for every active registry
member of the community that has none; on a clean community there are no members yet, and
every member who entered through onboarding already has one.

- The registry community must already exist. The sync reads it first and answers `404
  community_not_found` when it does not (REQ-0013), before calling the reconcile; a
  reconcile that still answers `community_not_found` is reported as a failed step.
- The step is reported as `setup: {status, code, reason, members, created}`: `succeeded`
  with the reconcile's member and created counts, `failed` with the provisioning service's
  `code` when it gave one and a reason naming the status (never its message), `skipped`
  without `PROVISIONING_URL`, `not_run` in a dry run.
- **A failed or unconfigured reconcile does not stop the area writes.** When the reconcile
  fails (any error answer, an unreachable provisioning service, a refused token) or is not
  configured (no provisioning service URL set), the sync still writes the areas and topology,
  and its answer reports the set-up step as `failed`, with the reason, or `skipped`. The
  route's status reflects the area writes, not the set-up step.
- **A re-run completes it.** The reconcile creates only what is missing, and the area writes
  are idempotent (REQ-0011), so running the sync again once the provisioning service answers
  sets the community up and changes no area.
- This service stays the only caller of the provisioning service.

### REQ-0015 — the console shows whether the registry's areas match the template

Per REC with a `rec_registry` block, the operator console shows whether the registry
community's areas and topology match the template's areas, and which differ, so an area
reintroduced by a bundle import is visible without running a sync.

- The check reads the registry community (its areas and topology) with this service's own
  registry read, `rec-registry.read`, which the registry's community `GET` routes require. No
  new scope is introduced for it, and it writes nothing.
- `GET /api/admin/recs/{rec_slug}/registry-drift`, capability `recs.drift` on that REC. It
  answers `status: matches | drift | not_synced` (a template whose areas are not
  boundaries) and each area's `state`: `matches`, `missing` (not in the registry, a renamed
  area included until the sync moves it), `differs` (its name, boundary or topology), or
  `undeclared` (in the registry, not in the template), with the registry areas holding its
  boundary under another key. It asks the Digital Twin nothing.
- **Who sees it (D55): the `platform-admin` role, and that REC's own `managers` and
  `admins`.** Not the REC's `editors` or `viewers`, no realm group (REQ-0030), and no
  service account: no scope grants `recs.drift`, `onboarding.admin` included. The console shows its *Areas* page, and the link to it, only to a caller
  whose `GET /api/admin/me` lists `recs.drift` for the REC.

### REQ-0021 — a template area may carry a display name, which the sync writes as the registry area's and its node's name

A boundary area in a template may give an optional `name` beside its `boundary`:

```yaml
rec_registry:
  community: example-rec
  areas:
    north:
      name: North valley
      boundary: {source: gse_cabine_primarie, id: AC000E00000}
```

- **The name is the area's display name; the key stays the identifier.** The registry sync
  writes it as the registry area's `name` and as its topology node's `name` (REQ-0011). An
  area that gives none is named by its key, as before.
- **It is a string of 1 to 128 characters with no surrounding spaces.** Anything else —
  empty, blank, padded, longer, not a string — is refused at template import and by the sync
  (`422 template_invalid`, REQ-0013).
- **A changed name is a changed area and node.** A sync after the name changes reports both
  `changed` and writes them; a sync after that is a no-op. The drift check reports an area
  whose registry name differs as `differs`.
- **The name decides nothing else.** A member's area, the boundary lookup and the registry
  area key are the key; the name is not logged.
