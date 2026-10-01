# API Reference

**Public (session-gated, rate-limited):**

Every per-community route is under `/api/{rec}`, where `{rec}` is the template's slug; an
unknown slug is `404`. Only the cross-community routes, downloads, `/api/me/**` and
`/api/health` are not.

| Method | Path | Auth | Notes |
|---|---|---|---|
| `GET` | `/api/health` | none | Liveness |
| `GET` | `/api/recs` | none | Every community this deployment serves, for the landing page |
| `POST` | `/api/recs/find-by-address` | none | The coverage check across every community (`RATE_LIMIT_ELIGIBILITY`, shared with the per-community check); see below |
| `GET` | `/api/{rec}/config` | none | Template config, `login_invitation` and `features` |
| `GET` | `/api/{rec}/template/{path}` | none | A template asset (logo, content) |
| `GET` | `/api/{rec}/sharing-offers` | none | Data-sharing offers for the wizard, proxied from the connector's `/ns/sharing-offers` and filtered by the manifest allow-list |
| `GET` | `/api/{rec}/consent-documents` | none | The community's local consent documents' metadata |
| `GET` | `/api/{rec}/consent-documents/{slug}` | none | PDF or redirect (`/meta` for its metadata) |
| `POST` | `/api/{rec}/eligibility` | none | Coverage check (`RATE_LIMIT_ELIGIBILITY`, default 30/hr per client address); see below |
| `POST` | `/api/{rec}/submissions` | none | Create (consent-first), returns session token (20/hr) |
| `GET/PATCH` | `/api/{rec}/submissions/{id}` | session | Read/update own (10min TTL). `PATCH` edits a draft only: once submitted it answers `409` for any field |
| `POST/GET` | `/api/{rec}/submissions/{id}/documents` | session | Upload, list (10min TTL) |
| `GET` | `/api/{rec}/submissions/{id}/pdf` | session | Download summary (10min TTL, 5/min) |
| `POST` | `/api/{rec}/extract` | session | Bill OCR (10/hr) |
| `POST` | `/api/{rec}/extract-id` | session | ID card OCR (10/hr) |
| `POST` | `/api/{rec}/documents/{id}/extract` | session | Extract from uploaded doc (ownership check) |
| `POST` | `/api/{rec}/extractions/{id}/confirm` | session | Confirm extraction (ownership check) |
| `POST` | `/api/{rec}/submissions/{id}/verify-phone` | session | Send SMS OTP (10/hr) |
| `POST` | `/api/{rec}/submissions/{id}/confirm-phone` | session | Confirm OTP, mark verified (20/hr) |
| `GET` | `/api/downloads/{token}` | token | Time-limited document download |

**The coverage check for a community whose areas are boundaries.**
`POST /api/{rec}/eligibility` takes `{"address"}` or `{"lat", "lng"}` as before and
answers `{"eligible": bool, ...}`. The geocoded point goes to the Digital Twin's
`boundary_at_point` with this service's own token (`svc-onboarding`,
`digital-twin.values.read`; the caller sends no credential and receives no token).
`matched_rule` and `matched_value` are always `null` and `reason` names nothing:
the answer carries no boundary id and no area. **No answer carries a coordinate**, for
any community: there is no `lat` or `lng` in it (removed in 0.4.0), whether the caller
sent an address or a point — the point geocoded from an address decides the answer and
is dropped (REQ-0006). `find-by-address` carries none either (REQ-0019). A Digital Twin that does not answer
is **503** with `detail` "We cannot check your address right now. Please try again
later.", as an unreachable geocoder is 503 — never `eligible`. Over the rate limit
the caller gets **429** and no geocoder or Digital Twin call is made.
`POST /api/recs/find-by-address` takes the same body and answers `{"matches": [{"slug",
"name", "branding", "locale", "matched_rule", "matched_value"}, …], "unchecked": bool}`
(`400` with neither an address nor a point, `404` for an address the geocoder cannot
place, `503` when the geocoder does not answer). It lists a boundary community only when the address is inside one of its
areas, with no matched rule. When the Digital Twin does not answer, only the boundary
communities fail closed: each is left out of `matches` (never listed on a guess),
`unchecked` is `true`, and every other community is still answered, so the route stays
**200**; the finder page then asks to try again later rather than saying nothing covers
the address. It shares the eligibility rate limit (REQ-0019).

**Submitting, for a community whose areas are boundaries.** `PATCH
/api/{rec}/submissions/{id}` (and the admin `PATCH` and `transition`) resolve the
submission's boundary from its supply address: the one the wizard checked,
`supply_address: {"text": "<address>"}` (saved by the eligibility step; the free-text
query the geocoder takes, 1 to 300 characters, no other key accepted), or, when
there is none, the scanned `extracted_data.indirizzo` (REQ-0018). No request field
sets `supply_boundary_id` or `supply_boundary_source`, and a value sent is ignored. A move to `submitted` is **422** when there is no supply address
or it falls in no boundary of the template's areas, and **503** when it cannot be
checked right now. The session read (`SubmissionRead`) carries the applicant's own
`supply_address` but not the boundary.

The four extraction routes, and an upload with `doc_type` `utility_bill` or
`id_card`, answer **403** with `{"detail": {"code": "document_processing_disabled", ...}}`
while document upload and scanning are off (`EXTRACTION_ENABLED`, `LLM_BASE_URL` and
`LLM_VISION_MODEL` not all set). They stay registered, so the contract has the same shape on every
deployment. `GET /api/{rec}/config` reports the state in `features.document_upload`
and `features.document_scan`.

**The member's own surface (`/api/me/**`, Keycloak identity, no capability):**

The only self-service surface in this service, and the only authenticated one
that names no `Capability`. The member's token is the whole authority and the
credential presented to the dataspace is the member's own — a route that let an
operator decide on somebody's behalf would defeat the point of recording consent.
Mounted **before** the `/api/{rec}` routers, because `{rec}` would otherwise
match `me`.

The REC is not in the path: a member does not choose which community they are in,
their token says, and accepting it from the caller would let anyone ask about any
community's offers.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/me/data-sharing` | Every offer this member's community publishes, with their decision on it, **merged across the connectors that hold them** — the community's own, and any participant holding data an offer reaches. **Provisions a dataspace identity** for a preregistered member who holds none — see below. `state` says why `has_identity` is false; `identity` carries the DID, role and dates. Each offer carries `decided_version`/`outdated`; `presented_version` — the version the onboarding form showed it at, accepted or not, so a caller can tell a decline from an offer never asked; `holder` — the participant that recorded the decision, absent for the community's own connector; `missing_prerequisites` — what that connector says this decision is still waiting for (`requires_offers`), which is how a granted offer that admits nobody yet explains itself; and `state` — `granted`, `withdrawn`, or `pending` while the connectors holding the offer's data disagree (`granted` stays `true` while pending: a grant stands somewhere). A holder that cannot be reached **fails the request**: a granted decision rendered as ungranted invites re-granting and hides a withdrawal |
| `POST` | `/api/me/data-sharing/{offer_id}` | Grant or withdraw one offer. Decided at the community's own connector as the member themselves, or relayed by the community to the participant that holds the data — as the member's own decision either way. An offer whose data is held in several places is decided at **every** one of them, each attempted before a failure is reported (`503`), so a withdrawal one connector refuses still reaches the others (ADR-0007). The decision is recorded by onboarding **before** any connector is called, and kept whatever they answer — it is what the operator's retry carries to a connector that refused; `503` without relaying anything if it cannot be recorded. `409` when the offer is not consent-based, not published by this REC, or the member is in a state with nothing to decide |
| `GET` | `/api/me/data-sharing/history` | The member's own provenance record. Empty when `DS_PROVENANCE_URL` is unset. **Never provisions** — a member with no identity has no history |

`state` is one of:

| Value | Meaning |
|---|---|
| `ok` | Offers listed, decisions merged, controls live |
| `no_dataspace` | This member's community does not take part, so there is nothing to decide and nothing to provision |
| `no_identity` | The community takes part and provisioning did not produce a usable credential. After the read route it means issuance was attempted and failed, or the member's token names no realm |
| `identity_conflict` | The identity registry answered `409`. Terminal for the member — retrying cannot clear it — and logged for an operator, who is the only one who can |
| `ambiguous_community` | The member is in more than one participating community, so "their offers" has no single answer. Refused rather than guessed |

`has_identity` is kept beside `state` because `../celine-webapp` and the page
built on it already read it. `state` is the additive half that says *why*.

`identity` is `null` unless `state` is `ok`, and otherwise carries `did`, `role`,
`issued_at` and `expires_at` — enough for a member to quote to a REC manager
looking them up, and the only way a member learns a DID minted on their behalf.
The dates are there because "my sharing stopped working" and "my credential
expired last week" are one event and only one of them is visible to the person.

**The read route also reconciles the export join.** `Member.did` in the REC
registry is what connects the connector's answer to *who consented* to the
registry's answer to *what they hold*; enablement writes it for a member the
funnel approved, and this route writes it for everyone else. Idempotent, never
fatal to the request, and detailed in [data-sharing.md](data-sharing.md).

**The read route provisions.** Where a member's community takes part and they
hold no presentable credential, `GET /api/me/data-sharing` issues one on the
strength of the REC's preregistration and re-resolves. This is a write behind a
`GET`, which is unusual and deliberate: members admitted offline hold no
submission, so the door they are standing at is the only one they have. (On a
deployed realm every member enters through this service, so a member admitted
offline is a local-development case — [ADR-0011](decisions/ADR-0011-on-a-deployed-realm-every-member-enters-through-onboarding.md).) It is
guarded by the resolve that precedes it — ds's own per-role idempotency is a
floor, and the resolve asks the stronger question of whether the credential can
be read back — and it never runs for a community outside the dataspace, which is
what `no_dataspace` is for.

**No response here ever carries a credential.** Not `vc_jws`, which authenticates
as the member, and not in any field added later.

**Admin console (`/api/admin/**`, Keycloak identity, capability-gated, audit-logged):**

Authorization is by organization + group for operators and by scope for service
accounts — see [authorization.md](authorization.md). The capability each
endpoint needs is in brackets.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/admin/me` | Identity + per-community capabilities. 403 when the caller administers nothing, which is what drives the console's denied page |
| `GET` | `/api/admin/recs` | Communities the caller may administer |
| `POST` | `/api/admin/recs/reload` | Force a manifest cache refresh (deployment-wide, so realm `admins`/`managers` only) [`recs.read`] |
| `POST` | `/api/admin/recs/{rec}/registry-sync?dry_run=&prune=` | Push the REC's template areas to its registry community, after setting the community up through the provisioning reconcile. **Realm `admins` only**, no scope grants it; see below [`recs.write`] |
| `GET` | `/api/admin/recs/{rec}/registry-drift` | Whether the registry's areas and topology match the template; a read, see below. Realm `admins` and the REC's own `managers`/`admins` only, no scope [`recs.drift`] |
| `GET` | `/api/admin/{rec}/stats` | Queue counts by status + submissions with a failed enablement step [`submissions.read`] |
| `GET` | `/api/admin/{rec}/submissions` | Queue. Filters `status`, `ref`, `created_from/to`; `X-Total-Count` header. Fiscal code and POD masked [`submissions.read`] |
| `GET` | `/api/admin/{rec}/submissions/{id}` | One submission. `?reveal=true` unmasks, needs [`submissions.reveal`] and is audited as its own action. Carries `supply_boundary_id`, `supply_boundary_source`, `supply_boundary_area` (the key of the area of the template in force whose boundary it is, or `null`) and `supply_boundary_area_name` (that area's display name, its key when the template gives none, or `null`; REQ-0023) |
| `PATCH` | `/api/admin/{rec}/submissions/{id}` | Edit fields and notes. From `submitted` on, `pod_code`, `first_name`, `last_name` and `email` answer `409`: they are corrected by revision [`submissions.write`] |
| `GET` | `/api/admin/{rec}/submissions/{id}/revisions` | Every correction of the POD, names and email, oldest first; per field the newest is in force. POD values masked; `?reveal=true` unmasks, needs [`submissions.reveal`] and is audited [`submissions.read`] |
| `POST` | `/api/admin/{rec}/submissions/{id}/revisions` | Correct one field: `{"field", "value", "method": "offline"\|"uploaded-document", "document_id"?, "note"}`, note required. Updates the column; `409` unless submitted, under review or approved; `422` for a value the field refuses, the value already held, or a document not on this submission. After approval the value is then propagated, and the answer's `steps` say how far each target got ([admin console](admin-console.md#propagation)) [`submissions.revise`] |
| `POST` | `/api/admin/{rec}/submissions/{id}/revisions/{revision}/retry` | Re-run a revision's unfinished propagation steps, or one named step (`{"step"}`), then the steps waiting for it. `409` for a revision recorded before approval [`submissions.revise`] |
| `POST` | `/api/admin/{rec}/submissions/{id}/transition` | Drive the state machine. A reason is required when rejecting [`submissions.review`] |
| `DELETE` | `/api/admin/{rec}/submissions/{id}` | GDPR erasure (files + DB) [`submissions.purge`] |
| `GET` | `/api/admin/{rec}/submissions/{id}/enablement` | What approval did, step by step [`submissions.read`] |
| `POST` | `/api/admin/{rec}/submissions/{id}/enablement/retry` | Re-run unfinished steps, or one named step (`{"step": …}`) [`enablement.retry`]. Named, `keycloak_user` also re-runs a succeeded login whose invitation is `send_failed`, and `dataspace_share` re-examines a **succeeded** consent step, or one **skipped** because the member declined everything on the form: every connector holding the member's offers is brought to their newest decision — including the one recorded when they last pressed on their page — withdrawals included, and nothing is written where they agree ([data-sharing](data-sharing.md)). A connector, or the recorded decisions, that cannot be read fails the step and nothing is written |
| `POST` | `/api/admin/{rec}/submissions/{id}/enablement/revoke` | Reverse enablement [`enablement.revoke`] |
| `GET` | `/api/admin/{rec}/submissions/{id}/documents` | Uploaded documents [`submissions.read`] |
| `GET` | `/api/admin/{rec}/submissions/{id}/documents/{doc}` | Stream one, decrypted. Audited [`submissions.read`] |
| `GET` | `/api/admin/{rec}/submissions/{id}/pdf` | Summary PDF [`submissions.read`] |
| `POST` | `/api/admin/{rec}/submissions/{id}/retry-share` | **Deprecated** alias of `enablement/retry?step=dataspace_share` |
| `POST` | `/api/admin/{rec}/exports/csv` | Streamed CSV of the community's register, for its own use; names no recipient [`export`] |
| `POST` | `/api/admin/{rec}/exports/pod-list` | The community's dated evidence for one offer: supply points that stood authorised, and those whose members withdrew, in separate columns. Body: `{"offer_id"}` only — a `recipient_ref` sent by an older caller is ignored. Read at every connector holding the offer; 422 when its datasets disagree on who consents. Streams the CSV body; records no disclosure (ADR-0010) [`export`] |
| `GET` | `/api/admin/{rec}/audit-logs` | This community's trail only [`audit.read`] |
| `POST` | `/api/admin/communities/{community}/members/{member_key}/invitation` | Email a registry member an invitation to set a password. **Delegated**, see below [`members.invite`] |
| `POST` | `/api/admin/communities/{community}/members/{member_key}/password-reset` | Email a registry member a password reset. **Delegated**, see below [`members.invite`] |

**Registry sync (`/api/admin/recs/{rec}/registry-sync`, ADR-0012, ADR-0014):**

The template is the source of truth for a community's areas; this route is the only thing
that writes them to the REC registry, and it runs only when a realm admin calls it (never on
a template load). `onboarding-cli registry-sync --rec <slug> [--dry-run] [--prune]` calls
the same route with a realm admin's own `--token`, or runs `--local` in process under the
break-glass rules; the CLI's client-credentials identity is never used for it
([specification](specifications/registry-sync.md)).

In order:

1. **The template is validated as it is now**, with template import's checks, every boundary
   id against the Digital Twin included. Refusals: `422 template_invalid`, `422
   template_not_syncable` (no `rec_registry` block, or municipality-list areas), `503
   boundaries_unavailable`. The body is `{"detail": {"code", "message"}}`.
2. **The registry community is read** with this service's `rec-registry.read`:
   `503 registry_not_configured`, `404 community_not_found`, `502 registry_unavailable` or
   `registry_refused`. For each registry area the template does not declare, its members
   are counted (never returned).
3. **The community is set up** (not in a dry run): the provisioning service's `POST
   /reconcile/{community}`, with a token asking for the optional scope
   `provisioning.reconcile`. A failure, or no `PROVISIONING_URL`, is reported in `setup` and
   does not stop step 4.
4. **The writes** (not in a dry run), with a token asking for the optional scope
   `rec-registry.community.write`: each topology node `{id: <boundary id>, type:
   primary_substation, name: <area name>}`, then each renamed area (below) through the
   registry's `POST …/areas/{old_key}/rename` `{"new_key"}`, then, with `prune=true`, the
   undeclared areas that hold no member, then each area `{name: <area name>, boundary:
   {source, id}, topology: [<boundary id>]}`, then the topology nodes only a pruned area
   used. The area name is the template's optional `name` for the area, or its key. What the
   template does not own (a node's `operator_id`/`parent`, an area's `location`/`geometry`)
   is kept.

   **A renamed area** — a template key the registry does not have, whose boundary the
   registry holds under one key the template no longer declares — is moved by the registry
   in one request, with every member naming the old key; no `prune` is needed. It is listed
   once, under the new key, as `renamed` with `renamed_from` and `members` (how many moved).

The answer, `200` whenever the sync ran:

```json
{
  "rec": "rec-b", "community": "example-rec", "dry_run": false, "prune": false, "ok": true,
  "setup": {"status": "succeeded", "code": null, "reason": null, "members": 0, "created": 0},
  "nodes": [{"key": "AC000E00001", "boundary_id": "AC000E00001", "outcome": "created",
             "code": null, "reason": null, "members": null, "renamed_from": null}],
  "areas": [{"key": "north", "boundary_id": "AC000E00001", "outcome": "created",
             "code": null, "reason": null, "members": null, "renamed_from": null},
            {"key": "south", "boundary_id": "AC000E00002", "outcome": "renamed",
             "code": null, "reason": null, "members": 4, "renamed_from": "old-south"},
            {"key": "east", "boundary_id": null, "outcome": "undeclared",
             "code": null, "reason": null, "members": 3, "renamed_from": null}],
  "summary": {"nodes": {"created": 1}, "areas": {"created": 1, "renamed": 1, "undeclared": 1}}
}
```

- `outcome`: `created`, `changed`, `unchanged`, `refused` (with `code` and `reason`),
  `deleted`, `undeclared` (kept: no `prune`), `renamed` (moved from `renamed_from`, with
  `members` moved; in a dry run, counted), or `not_run` (the registry stopped answering).
  In a dry run it is what a real run would do.
- Refusal codes on an item: `area_in_use` (with `members`), `boundary_held` (the registry
  holds the boundary under another area key and the new key is already taken, so no rename
  is possible; `prune` removes the old one once it has no members),
  `topology_node_not_written`, or the registry's own code (a refused rename carries
  `area_key_taken`, `area_not_found` or `invalid_area_key`).
- `ok` is `false` when any item is refused or not run. The set-up step does not decide it:
  `setup.status` is `succeeded`, `failed` (with `reason`), `skipped` or `not_run`.
- A real run writes one audit row, `registry_sync` on entity `rec`, with counts only.

`GET /api/admin/recs/{rec}/registry-drift` answers `{"rec", "community", "status":
"matches" | "drift" | "not_synced", "areas": [{"key", "boundary_id", "state", "held_by"}],
"nodes": [{"key", "state"}]}`, `state` being `matches`, `missing`, `differs` or
`undeclared`. It reads with `rec-registry.read` and asks the Digital Twin nothing. It needs
`recs.drift`: a realm `admins`, or a `managers`/`admins` of the REC's own organization; not
its editors or viewers, not a realm `managers`, and no service account (D55).

**Member-keyed, delegated (`/api/admin/communities/**`):**

These two routes are how a community manager's "Send invitation" and "Reset password"
buttons on the `celine-community` dashboard reach the provisioning service. This service is
the provisioning service's only caller. Unlike the rest of the admin surface, they are keyed
on the registry's own pair, not on a REC slug and a submission. A member imported into the
registry is therefore as reachable as one onboarded here. On a deployed realm every member
enters through this service, so an imported member is a local-development case
([ADR-0011](decisions/ADR-0011-on-a-deployed-realm-every-member-enters-through-onboarding.md)).

- `{community}` is the **registry community key**: the manifest's `rec_registry.community`,
  not the slug. It resolves to exactly one REC. A key that no manifest declares is
  `404 community_not_served`. A key that two manifests declare is `409 community_ambiguous`,
  and it is logged as an authoring error.
- `{member_key}` is the registry member key. Both values reach the provisioning service
  unchanged.
- **No request body.** The route is the intent, so the reset route cannot send an invitation.
- **No submission is read and nothing local changes** except one audit row.

**Two tokens, both verified.** The caller is a service holding `onboarding.members.invite`,
presented in `Authorization: Bearer`. It forwards the manager's own access token in
`X-Acting-User-Token`. The policy allows the call only when the service holds the scope
**and** the manager holds `admins` or `managers` on the REC's organization, or at realm
level. A manager's token alone is refused, and so is any service alone, `onboarding.admin`
included. See [authorization.md](authorization.md#delegated-actions).

- A request that also carries `x-auth-request-access-token` is refused, because it came
  through the public ingress.
- Call these routes on the internal address only.

A `200` is `{"code", "kind", "lifespanSeconds"}`:
- `code` is `sent`, or `not_on_dev_list` when dev email mode held the email back;
- `kind` is `invitation` or `password_reset`.

Every refusal is `{"detail": {"code", "message"}}`. **Branch on `code`.** The message is
English, for logs, and never relays the provisioning service's own words.

| Status | `code` | From |
|---|---|---|
| `401` | `invalid_token` (the service token), `actor_token_invalid` (missing or invalid `X-Acting-User-Token`), `proxy_token_refused` | here |
| `403` | `forbidden`; the message is the policy's reason | here |
| `404` | `community_not_served` | here |
| `404` | `community_not_found`, `member_not_found`, `account_not_found` | provisioning, passed through |
| `409` | `community_ambiguous` | here |
| `409` | `account_disabled`, `no_email` (nothing sent), `has_password` (on an invitation), `no_password` (on a reset) | provisioning, passed through |
| `429` | `cooldown`, with `retryAfterSeconds` and a `Retry-After` header | provisioning, passed through |
| `502` | `send_failed` (retryable: a failed send starts no cooldown), `registry_unavailable`, `provisioning_failed` | provisioning, passed through |
| `502` | `provisioning_refused`: the provisioning service refused **this** service's credential, or it could not get one. A deployment fault, logged at `ERROR` | here |
| `503` | `provisioning_not_configured` (`PROVISIONING_URL` is empty), `provisioning_unavailable` (timeout or connection error), `admin_not_configured` | here |

A code not listed keeps its status and its code. A provisioning refusal that carries no code
arrives as `http_<status>`.

Every request that passes authorisation writes one audit row, including one the provisioning
service refused:
- `action` is `member_invitation` or `member_password_reset`;
- `entity_type` is `registry_member`, and `entity_id` is the member key;
- `actor_*` holds the **manager's** `sub` and email, and the **calling service's** client id;
- `detail` is `community=… code=… status=…`. The status is `none` when the provisioning
  service did not answer.

A denied request is logged and is not audited.

