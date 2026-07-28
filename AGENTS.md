# REC Onboarding Platform — Operational Setup

## Objectives

- Web platform for onboarding participants into Renewable Energy Communities (REC/CER)
- Designed for Italian CERs, extensible to other EU regions via templates
- OCR/LLM extraction from utility bills to pre-fill forms
- Privacy-first: consent before data collection, 10-minute session TTL, GDPR audit trail
- Template-driven customization per community (branding, coverage, consent docs, steps)

## Tech Stack

### Backend (`./src`)

- **Package:** `celine.onboarding` (namespace package — `celine/` has no `__init__.py`)
- **Framework:** FastAPI, async, with slowapi rate limiting
- **Models/config:** Pydantic v2, Pydantic Settings (`.env` driven)
- **Database:** PostgreSQL, Alembic migrations at repo root (`./alembic/`)
- **Extraction:** markitdown (PDF text) + OpenAI Vision API (images), model configurable
- **Validators:** Italian codice fiscale (checksum), POD code format — enforced in Pydantic schemas
- **Geocoding:** Nominatim (OpenStreetMap) for eligibility checks
- **Encryption:** Fernet (cryptography lib) for file + column encryption, key via `ENCRYPTION_KEY`
- **Package management:** uv + pyproject.toml (repo root) + hatchling build

### Frontend (`./ui`)

- **Framework:** SvelteKit 5 (runes syntax)
- **Styling:** CSS custom properties (`--celine-*`), DM Sans font, branding from template config
- **Package management:** pnpm with `ignore-scripts=true`
- **i18n:** sveltekit-i18n, Italian (default) + English
- **Rendering:** marked for markdown content from templates, sanitized with DOMPurify
- **API proxy:** Vite dev server proxies `/api` to backend

### Infrastructure

- **`./Dockerfile`** — backend (Python 3.12, uv)
- **`./ui/Dockerfile`** — frontend (Node 22, pnpm)
- **`./docker-compose.yml`** — init-db + migrate + backend + frontend (external Postgres)
- **`./Taskfile.yaml`** — run:api, run:ui, migrate, test, lint, export-csv, export-pod-list

## Project Layout

```
./
├── src/celine/onboarding/      # Python backend (namespace package)
│   ├── main.py                 # App factory, CORS, security headers, rate limiting
│   ├── api/
│   │   ├── admin.py            # Token-protected operator endpoints
│   │   ├── submissions.py      # Public endpoints (10min TTL)
│   │   ├── documents.py        # Upload/list (TTL-gated)
│   │   ├── extractions.py      # OCR extraction (session-gated, rate-limited)
│   │   ├── eligibility.py      # Coverage check (geocoding)
│   │   ├── phone_verify.py     # SMS OTP send/confirm (session-gated)
│   │   ├── consent_documents.py # PDF/URL serving
│   │   ├── config.py           # Template config + assets
│   │   ├── downloads.py        # Token-authenticated document download
│   │   └── deps.py             # Shared: limiter, admin auth, session auth
│   ├── models/                 # SQLAlchemy + Pydantic schemas
│   │   ├── encrypted.py        # EncryptedString + EncryptedJSON TypeDecorators
│   │   └── ...
│   ├── services/               # Business logic, email, PDF, templates, eligibility, sms + otp
│   ├── extractors/             # OpenAI Vision + markitdown
│   ├── validators/             # CF checksum, POD format
│   ├── workflows/              # Status state machine
│   ├── outputs/                # Storage backends (S3, Google Drive) + webhook
│   └── config/settings.py      # All env vars
├── alembic/                    # Migrations (repo root)
├── templates/                  # Per-community customization
│   ├── example/                # Default template
│   └── <slug>/                 # Custom: manifest.yaml, consent/, content/, assets/
├── ui/                         # SvelteKit frontend
├── data/                       # All user data (gitignored)
│   ├── submissions/<ref>/      # Uploaded files (YYYYMMDD-shortid)
│   └── exports/                # CSV exports
├── pyproject.toml              # Root (uv + hatch)
├── docker-compose.yml
├── Taskfile.yaml
└── .env.example
```

## Configuration

All settings are driven by environment variables, loaded via Pydantic Settings from `.env`. Settings have sensible defaults where safe; security-critical values have no default and must be set explicitly.

### Required

| Variable | Notes |
|---|---|
| `DATABASE_URL` | Async PostgreSQL connection string. No default — app will not start without it. Example: `postgresql+asyncpg://user:pass@host:5432/db` |
| `OPENAI_API_KEY` | Required for bill/ID extraction. No extraction without it. |
| `ENCRYPTION_KEY` | Fernet key for PII encryption at rest (files + DB columns). Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. App refuses to start without it unless `REQUIRE_ENCRYPTION=false`. |
| `DPA_SIGNED` | Set to `yes` when the manifest includes LLM extraction steps (`utility`/`identity`). Requires a signed Data Processing Agreement with your LLM provider (GDPR Art. 28). App refuses to start without it. |
| `DPA_SMS_SIGNED` | Set to `yes` when `SMS_PROVIDER` is a real gateway (not `log`). Requires a signed DPA with the SMS provider (GDPR Art. 28). App refuses to start otherwise. |
| `ADMIN_TOKEN` | Bearer token for `/api/admin/*`. Admin endpoints return 503 if unset. |

### Security

| Variable | Default | Notes |
|---|---|---|
| `REQUIRE_ENCRYPTION` | `true` | Set `false` for local development without an encryption key. When `true`, app refuses to start without `ENCRYPTION_KEY`. |
| `SECURITY_HEADERS` | `true` | Adds X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy to all responses. Set `false` if your reverse proxy handles these. |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:5173` | Comma-separated allowed origins. |
| `DOWNLOAD_TOKEN_TTL` | `86400` | Seconds before download links expire (default: 24 hours). |

### Application

| Variable | Default | Notes |
|---|---|---|
| `TEMPLATES_DIR` | `./templates` | Root directory `task import-templates` reads community templates from. |
| `DATA_DIR` | `./data` | Where uploads and exports are stored. |
| `MAX_UPLOAD_SIZE_MB` | `10` | Max file size per upload. |
| `EXTRACTION_BASE_URL` | `https://api.openai.com/v1` | Override for OpenAI-compatible APIs. |
| `EXTRACTION_MODEL` | `gpt-5.4` | Model used for OCR extraction. |

### Email (SMTP)

All optional. If `SMTP_HOST` is unset, email notifications are silently skipped.

| Variable | Default | Notes |
|---|---|---|
| `SMTP_HOST` | *(none)* | SMTP server hostname. |
| `SMTP_PORT` | `587` | SMTP port. |
| `SMTP_USER` | *(none)* | SMTP username. |
| `SMTP_PASSWORD` | *(none)* | SMTP password. |
| `SMTP_FROM` | *(none)* | Sender address (overridden by manifest `notifications.from`). |
| `SMTP_TLS` | `true` | STARTTLS with certificate verification via `ssl.create_default_context()`. |
| `SMTP_NOTIFY` | *(none)* | Fallback operator email list (overridden by manifest `notifications.notify`). |

### Dataspace sharing

Optional. Enable data-sharing consent collection in the wizard and its provisioning to the dataspace connector on approval. Both empty by default; `DS_CONNECTOR_URL` empty disables share provisioning entirely. See [docs/data-sharing.md](docs/data-sharing.md).

| Variable | Default | Notes |
|---|---|---|
| `DS_CONNECTOR_URL` | *(none)* | Connector base URL used to provision standing consent (`POST /consent/admin/shares`) after approval. Empty disables share provisioning. |
| `DS_NS_URL` | *(none)* | Public vocabulary base (`GET /ns/sharing-offers`) the wizard renders offers from. Empty falls back to the connector's `/ns` path. |
| `REC_REGISTRY_URL` | *(none)* | REC registry base URL. Empty disables member registration. Which community and area is per-REC, in the manifest's `rec_registry:` block |
| `DS_PROVENANCE_URL` | *(none)* | Provenance base URL for recording an offline CSV export as a `DataDisclosed` event (`POST /prov/events`, scope `provenance.write`). Empty disables the emission; the export still runs. |

**The per-community binding is not an env var.** Which dataspace organization a
REC's members belong to lives in that REC's `manifest.yaml`, under `dataspace:`
(see Template System below). It is per community because this platform is
multi-tenant; as deployment settings it filed every community's members into one
organization, silently. There is deliberately no deployment-wide fallback —
leaving one would let the defect return the first time a manifest was written
without a block.

**Startup refuses a dataspace misconfiguration.** A REC declaring
`consent.data_sharing` with neither `DS_NS_URL` nor `DS_CONNECTOR_URL` set, or a
REC bound to an organization the identity registry does not know, fails at boot
rather than mid-review. A registry that cannot be *reached* does not block boot —
"no such owner" is a configuration error, "I could not ask" is not.

**Supply-point list (Block C).** `task export-pod-list -- --rec <slug> --offer <id> --recipient <ref>` writes the PODs of members holding a current consent for that offer, one column and nothing else, plus a `DataDisclosed` event. It is a snapshot, so the re-export cadence is the revocation latency; the header says so.

**Offline disclosure provenance (Block C).** `task export-csv -- --recipient <ref> [--purpose …] [--agreement-ref …]` records the export as a `DataDisclosed` provenance event via `services/dataspace_identity.emit_data_disclosed`. It carries **codes, DIDs and hashes only, never PII** — `columns` are field *names* and a `consent_snapshot_hash` (offer-level, since dataset resolution lives in the connector) fingerprints the consent state. The emit is non-fatal: a provenance failure never fails the export. Without `--recipient`, nothing is emitted. See [docs/data-sharing.md](docs/data-sharing.md).

## Template System

Each community gets a `templates/<slug>/` folder:

```yaml
# manifest.yaml
slug: my-rec
name: "My Energy Community"
branding:
  primary_color: "#2d6a4f"
  logo: assets/logo.svg
fields:
  extra:
    - key: has_pv
      label: "Ho un impianto fotovoltaico"
      "label:en": "I have a photovoltaic system"
      type: boolean          # boolean | text | number | select
      step: energy            # which wizard step this appears in
    - key: pv_kwp
      label: "Potenza impianto (kWp)"
      type: number
      step: energy
      suffix: kWp
      show_if: { key: has_pv, value: true }  # conditional visibility
    - key: has_battery
      label: "Ho un sistema di accumulo"
      type: boolean
      step: energy
      show_if: { key: has_pv, value: true }
    - key: battery_kwh
      label: "Capacita' batteria (kWh)"
      type: number
      step: energy
      suffix: kWh
      show_if: { key: has_battery, value: true }
    - key: has_ev
      label: "Ho un'auto elettrica"
      type: boolean
      step: energy
    - key: has_heat_pump
      label: "Ho una pompa di calore"
      type: boolean
      step: energy
    - key: cassa_rurale_member       # community-specific extra question
      label: "Sono socio della cassa rurale"
      type: boolean
      step: personal
  hidden: []
consent:
  gdpr: { version: "1.0", url: "https://..." }      # external URL
  policy: { version: "1.0", file: consent/policy.pdf } # local file
  statute: { version: "1.0", url: "https://..." }
  data_sharing:                                     # optional; collected in the statute step
    required: false                                 # GDPR Art. 7(4): NEVER required, never blocks submission
    offers: [household-energy-flexibility]          # optional allow-list; omit to offer every consent-based offer the connector publishes
    # No version/file here — the version comes from each offer's consent_text_version served by the connector.
coverage:
  rules:
    - type: municipality
      values: [Town A, Town B]
    - type: postal_code
      values: ["12345", "12346"]
steps: [consents, utility, personal, energy, eligibility, statute, review]
notifications:
  from: "noreply@my-rec.org"
  notify: [admin@my-rec.org]
  base_url: "https://my-rec.example.com"   # base URL for download links in emails
  email: true                               # set false to disable email notifications
  storage:                                  # optional: upload submissions to external storage
    backend: s3                             # s3 | gdrive
    bucket: "${S3_BUCKET}"                  # env var interpolation with ${VAR}
    access_key_id: "${S3_ACCESS_KEY_ID}"
    secret_access_key: "${S3_SECRET_ACCESS_KEY}"
    region: eu-south-1
    prefix: submissions
    url_expiry_seconds: 604800
  webhook:                                  # optional: POST on submission
    url: "https://hooks.example.com/onboarding"
    secret: "${WEBHOOK_SECRET}"             # HMAC-SHA256 signature in X-Signature-256
content:
  welcome: content/welcome.md
  consent_intro: content/consent_intro.md
  success: content/success.md
```

Imported into the `Rec` table with `task import-templates`, then served per community at `/{rec}` — one deployment hosts several.

### REC registry binding (optional, per community)

```yaml
rec_registry:
  community: example-community       # community key in the REC registry
  area: north                        # one of that community's own area keys
```

`area` is a **fixed configured value** — every member of this community is
registered into it, and a REC manager moves people from there.

That is a deliberate placeholder. Assigning a member to the right area means the
community's areas being 1-1 with the registry's *and* geocoding the supply
address against their geofences. That data exists but is not carried here yet,
and a half-way heuristic — matching a municipality name to an area key — would be
quietly wrong now and plainly wrong the moment a second community exists. A wrong
area is also sticky: the registry refuses to delete an area while members
reference it.

Omit the block and approved participants are not registered; the wizard still
works. Requires `REC_REGISTRY_URL`, and startup refuses a REC that declares the
block without one.

### Dataspace binding (optional, per community)

```yaml
dataspace:
  organization: example-community          # = KC org alias = IR owner id
  organization_did: did:web:example-community.dataspaces.localhost
  linked_participant_did: did:web:consumer.dataspaces.localhost
  membership_role: member                  # optional
```

`organization` is **one identifier** across the platform: the owner `id` in the
deployment's `owners.yaml`, the Keycloak organization alias, and the owner id in
the identity registry. No mapping table. `task import-templates` validates it,
and it is **required** whenever the block is present — a credential with no
membership is an identity the consent endpoints will not act on.

Omit the block and the community is not in the dataspace: the full wizard runs,
no sharing consent is collected, no identity is provisioned. Supported, not
degraded — onboarding works with no dataspace infrastructure at all.

The organization must already exist and be promoted in the registry. **Onboarding
never creates one**: an organization minted from an approval carries no
verification and no agreement, so it declares no capacity — and capacity is what
decides whether a recipient is disclosed or must be consented to separately. See
[docs/dataspace-integration.md](docs/dataspace-integration.md).

## What approval does

Approving a submission enables somebody, which means three things land **in this
order**:

| # | Effect | Where | On failure |
|---|---|---|---|
| 1 | Login identity | Keycloak user | fails closed |
| 2 | Community member | `rec-registry` | **fails closed** |
| 3 | Dataspace identity + standing consent | identity registry + connector | identity fails closed; the consent share does not |

The order is load-bearing. The registry keys a member on `(community, user_id)`,
so the Keycloak user has to exist first; the dataspace identity is last because
it is the step that can be retried afterwards.

**Registry registration fails closed, unlike share provisioning.** A missing
consent row is recoverable — it has a retry endpoint. A participant missing from
the registry is enabled in name only: invisible to every pipeline, dashboard and
digital-twin query, all of which join on the registry's `user_id`, POD and sensor
ids. That is not a state anything downstream can work around.

An already-registered participant (`409`) is **not** a failure: approval is
retriable, and refusing the second attempt would leave a submission that can
never be approved.

What is derived, and what is not:

- `role` — `prosumer` when the energy step reports PV, else `consumer`. A
  community that asks different questions gets `consumer`, the safe reading.
- `area` — the configured one. Not derived from the address; see above.
- `delivery_points` — the **POD**, which is the one thing that must be tracked
  from onboarding: the distributor keys on it, metering data arrives against it,
  and unlike a meter it is known before any device is installed.
- **No assets are registered at all.** What the wizard collects is self-stated —
  ticking "I have a photovoltaic system" is a declaration, not a commissioned
  installation, and registering it as an asset would make an unverified claim
  indistinguishable from a surveyed one. A meter cannot be registered here in any
  case: its `sensor_id` is assigned when the device is physically installed,
  after onboarding. Asset registration is the REC manager's offline work.
  The answers are kept under the member's `extra.declared_at_onboarding`, so a
  manager deciding what to survey does not have to ask again.

## Wizard Flow

1. **Consents** — GDPR + policy + keep-me-updated. Creates the submission (UUID, IP, timestamps).
2. **Bill Upload** — Optional multi-page upload. AI extraction produces editable prefilled data.
3. **Personal Data** — Name, email, phone, CF, POD (validated, prefilled from extraction) + manifest extra fields. Optional ID card upload with cross-validation against bill data.
4. **Energy System** — PV, battery, EV, heat pump questions (manifest-driven, with conditional visibility).
5. **Eligibility** — Address geocoded and checked against coverage rules (if configured).
6. **Statute** — Separate consent for community statute. Also collects **optional data-sharing consent** when the manifest declares `consent.data_sharing`: offers are rendered from `GET {DS_NS_URL}/ns/sharing-offers` (consent-based offers show a toggle; contract-based offers are disclosed without one), and the SHA-256 of the exact consent text shown is recorded. Placed here — not in the consents step, which runs before any data is collected and would be uninformed consent.
7. **Review** — Summary of all entered data. Submit triggers PDF generation + email notification.

Data-sharing consent is **optional** (GDPR Art. 7(4)): it is never required and never blocks `can_submit()`. On the submission it records `data_sharing_consent`, `data_sharing_consent_at`, `data_sharing_consent_offer_ids`, `data_sharing_consent_text_version`, `data_sharing_consent_locale`, `data_sharing_consent_text_sha256`, and `share_provisioned` (whether the consent was pushed to the connector). On approval these offers are provisioned to the dataspace connector — see [docs/data-sharing.md](docs/data-sharing.md) and [docs/dataspace-integration.md](docs/dataspace-integration.md).

## Security Model

### Encryption at rest

All PII is encrypted at the application layer using Fernet symmetric encryption (`ENCRYPTION_KEY` env var):

- **File encryption**: uploaded documents (utility bills, ID cards) are encrypted before writing to disk. Decrypted transparently on read.
- **Column encryption (EncryptedString)**: `first_name`, `last_name`, `email`, `phone`, `fiscal_code`, `pod_code`, `consent_ip` are encrypted in the database via the `EncryptedString` SQLAlchemy TypeDecorator.
- **JSON encryption (EncryptedJSON)**: `extracted_data`, `id_extracted_data` (on submissions), and `extracted_data`, `raw_response` (on extractions) are serialized to JSON, encrypted, and stored as text.
- **Backwards-compatible reads**: decryption gracefully handles legacy unencrypted data (returns value as-is on `InvalidToken`).
- **Mandatory in production**: the app refuses to start without `ENCRYPTION_KEY` unless `REQUIRE_ENCRYPTION=false` (dev-only escape hatch).

### Session and authentication

- **Session tokens**: 32-byte random tokens (`secrets.token_urlsafe`) generated on submission creation. Sent via `X-Session-Token` header. Tied to a single submission. 10-minute inactivity TTL.
- **Extraction endpoints** (`/api/extract`, `/api/extract-id`, `/api/documents/{id}/extract`, `/api/extractions/{id}/confirm`) require a valid session token. Document/extraction endpoints also verify ownership (document must belong to the caller's submission).
- **Admin endpoints** (`/api/admin/*`) require `Authorization: Bearer <ADMIN_TOKEN>`. Token comparison is timing-safe (`secrets.compare_digest`).
- **Download tokens**: Fernet-encrypted submission IDs with a configurable TTL (`DOWNLOAD_TOKEN_TTL`, default 24 hours).

### HTTP security headers

When `SECURITY_HEADERS=true` (default), all responses include:
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=()`

### Other protections

- **Rate limiting**: `/api/extract` and `/api/extract-id` 10/hr per IP, `/api/submissions` POST 20/hr, PDF 5/min.
- **Email hardening**: notification emails contain only the submission ref (no PII in body). STARTTLS enforced with certificate verification via `ssl.create_default_context()`.
- **Audit logging**: all admin operations logged to `audit_logs` table with action, entity, IP, and detail. Viewable via `GET /api/admin/audit-logs`.
- **GDPR erasure**: `DELETE /api/admin/submissions/{id}` deletes files from disk, removes all DB records (CASCADE), and logs the deletion.
- **Path traversal protection**: resolved paths validated against root directories.
- **MIME validation**: magic bytes, not client headers.
- **Input validation**: CF checksum + POD format enforced in Pydantic schemas.
- **XSS prevention**: markdown content sanitized with DOMPurify before rendering.
- **CORS**: configurable origins, restricted methods/headers.
- **No public list endpoint**: submission enumeration only via admin token.
- **Public API does not expose `consent_ip`**: IP address is only visible via admin endpoints (`SubmissionAdminRead` schema).

## API Summary

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

**Admin (token-protected, no TTL, audit-logged):**

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/admin/submissions` | List all (includes consent_ip) |
| `GET/PATCH` | `/api/admin/submissions/{id}` | View/update (includes consent_ip) |
| `DELETE` | `/api/admin/submissions/{id}` | GDPR erasure (files + DB) |
| `GET` | `/api/admin/submissions/{id}/pdf` | Download summary |
| `POST` | `/api/admin/submissions/{id}/retry-share` | Re-run data-sharing provisioning to the connector (`raise_on_error=True`; 422 on connector rejection) |
| `GET` | `/api/admin/audit-logs` | Paginated audit trail |

## Local Development

```bash
task sdk:local        # dev-only: celine-sdk from the sibling checkout (see below)
task run:api          # FastAPI on :8000
task run:ui           # SvelteKit on :5173 (proxies /api)
task migrate          # alembic upgrade head
task migration -- "description"
task test             # backend + frontend
task lint             # ruff + svelte-check
task export-csv       # CSV to ./data/exports/
task export-pod-list  # consented supply points for a distributor
```

### celine-sdk, temporarily

REC registry registration calls wrapper methods (`RecRegistryAdminClient.create_member`
and the member sub-resources) that were added alongside this integration and are
**not released yet**. `pyproject.toml` deliberately does not point at the local
checkout — a published release is what makes the dependency reproducible outside
this workspace — so until one ships:

```bash
task sdk:local        # uv pip install -e ../celine-sdk
```

`uv sync` reverts it, so re-run after syncing. If it is missing, startup refuses
with an explanation rather than letting the first approval fail on an
`AttributeError`. Bump the version constraint and delete both the task and that
startup check once celine-sdk releases.
