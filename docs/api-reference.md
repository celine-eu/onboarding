# API Reference

**Public (session-gated, rate-limited):**

| Method | Path | Auth | Notes |
|---|---|---|---|
| `POST` | `/api/submissions` | none | Create (consent-first), returns session token |
| `GET/PATCH` | `/api/submissions/{id}` | session | Read/update own (10min TTL) |
| `POST` | `/api/submissions/{id}/documents` | session | Upload (10min TTL) |
| `GET` | `/api/submissions/{id}/pdf` | session | Download summary (10min TTL) |
| `POST` | `/api/extract` | session | Bill OCR (10/hr) |
| `POST` | `/api/extract-id` | session | ID card OCR (10/hr) |
| `POST` | `/api/documents/{id}/extract` | session | Extract from uploaded doc (ownership check) |
| `POST` | `/api/extractions/{id}/confirm` | session | Confirm extraction (ownership check) |
| `POST` | `/api/{rec}/submissions/{id}/verify-phone` | session | Send SMS OTP (10/hr) |
| `POST` | `/api/{rec}/submissions/{id}/confirm-phone` | session | Confirm OTP, mark verified (20/hr) |
| `POST` | `/api/eligibility` | none | Coverage check |
| `GET` | `/api/config` | none | Template config |
| `GET` | `/api/{rec}/sharing-offers` | none | Data-sharing offers for the wizard, proxied from the connector's `/ns/sharing-offers` and filtered by the manifest allow-list |
| `GET` | `/api/consent-documents/{slug}` | none | PDF or redirect |
| `GET` | `/api/downloads/{token}` | token | Time-limited document download |

The four extraction routes, and an upload with `doc_type` `utility_bill` or
`id_card`, answer **403** with `{"detail": {"code": "document_processing_disabled", ...}}`
while document upload and scanning are off (`EXTRACTION_ENABLED` and
`EXTRACTION_API_KEY` not both set). They stay registered, so the contract has the same shape on every
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
| `GET` | `/api/me/data-sharing` | Every offer this member's community publishes, with their decision on it. **Provisions a dataspace identity** for a preregistered member who holds none — see below. `state` says why `has_identity` is false; `identity` carries the DID, role and dates |
| `POST` | `/api/me/data-sharing/{offer_id}` | Grant or withdraw one offer. `409` when the offer is not consent-based, not published by this REC, or the member is in a state with nothing to decide |
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
submission, so the door they are standing at is the only one they have. It is
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
| `GET` | `/api/admin/{rec}/stats` | Queue counts by status + submissions with a failed enablement step [`submissions.read`] |
| `GET` | `/api/admin/{rec}/submissions` | Queue. Filters `status`, `ref`, `created_from/to`; `X-Total-Count` header. Fiscal code and POD masked [`submissions.read`] |
| `GET` | `/api/admin/{rec}/submissions/{id}` | One submission. `?reveal=true` unmasks, needs [`submissions.reveal`] and is audited as its own action |
| `PATCH` | `/api/admin/{rec}/submissions/{id}` | Edit fields and notes [`submissions.write`] |
| `POST` | `/api/admin/{rec}/submissions/{id}/transition` | Drive the state machine. A reason is required when rejecting [`submissions.review`] |
| `DELETE` | `/api/admin/{rec}/submissions/{id}` | GDPR erasure (files + DB) [`submissions.purge`] |
| `GET` | `/api/admin/{rec}/submissions/{id}/enablement` | What approval did, step by step [`submissions.read`] |
| `POST` | `/api/admin/{rec}/submissions/{id}/enablement/retry` | Re-run unfinished steps, or one named step [`enablement.retry`] |
| `POST` | `/api/admin/{rec}/submissions/{id}/enablement/revoke` | Reverse enablement [`enablement.revoke`] |
| `GET` | `/api/admin/{rec}/submissions/{id}/documents` | Uploaded documents [`submissions.read`] |
| `GET` | `/api/admin/{rec}/submissions/{id}/documents/{doc}` | Stream one, decrypted. Audited [`submissions.read`] |
| `GET` | `/api/admin/{rec}/submissions/{id}/pdf` | Summary PDF [`submissions.read`] |
| `POST` | `/api/admin/{rec}/submissions/{id}/retry-share` | **Deprecated** alias of `enablement/retry?step=dataspace_share` |
| `POST` | `/api/admin/{rec}/exports/csv` | Streamed CSV; naming a recipient records a `DataDisclosed` event [`export`] |
| `POST` | `/api/admin/{rec}/exports/pod-list` | Consented supply points for one offer [`export`] |
| `GET` | `/api/admin/{rec}/audit-logs` | This community's trail only [`audit.read`] |

