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

**Admin console (`/api/admin/**`, Keycloak identity, capability-gated, audit-logged):**

Authorization is by organization + group for operators and by scope for service
accounts — see [authorization.md](authorization.md). The capability each
endpoint needs is in brackets.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/admin/me` | Identity + per-community capabilities. 403 when the caller administers nothing, which is what drives the console's denied page |
| `GET` | `/api/admin/recs` | Communities the caller may administer |
| `POST` | `/api/admin/recs/reload` | Force a manifest cache refresh (deployment-wide, so realm-level operators only) [`recs.read`] |
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

