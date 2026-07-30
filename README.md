# REC Onboarding Platform

A self-hosted web application for onboarding members into Renewable Energy Communities (REC). Built for Italian CERs (Comunita Energetiche Rinnovabili), designed to work across EU regions.

## Why

Joining a Renewable Energy Community involves collecting personal data, verifying utility contracts, and obtaining legal consents. Most RECs do this via paper forms, email exchanges, and manual data entry — error-prone and hard to scale.

This platform automates the process: a public-facing wizard collects data from applicants, extracts details from utility bills using AI, validates fields, checks geographic eligibility, and delivers a complete submission to the operator — with a full GDPR audit trail.

## How It Works

### For the applicant

1. **Accept consents** — GDPR privacy policy and community rules, with links to the actual documents. This step creates the submission and records the IP address, timestamp, and document versions.
2. **Upload utility bill** (optional) — photos or PDFs of the electricity bill. The system uses AI vision to extract the holder's name, fiscal code, POD code, address, and provider. Multiple pages can be uploaded; each one refines the extracted data.
3. **Confirm personal data** — a form pre-filled with extracted data. The applicant reviews and corrects. Fiscal code and POD are validated against their official formats. Optional ID card upload provides cross-validation against bill data.
4. **Eligibility check** (if configured) — the applicant's address is geocoded and checked against the community's coverage area (municipalities, postal codes, or regions).
5. **Accept statute** — the community's founding document, presented separately from the data-collection consents. If the community enables it, this step also offers an **optional data-sharing consent**: the applicant can authorise sharing specific offers into the dataspace. It is never required and does not block submission (GDPR Art. 7(4)).
6. **Review and submit** — summary of all entered data. On submit, the applicant receives a PDF summary and the operator is notified by email.

The entire process has a 10-minute inactivity window. After that, the session token expires and the public API rejects further requests. This limits the exposure window for personal data.

### For the operator

Operators access submissions via token-protected admin endpoints. They can list all submissions, review details, download PDF summaries, change status (submitted, under review, approved, rejected), and export to CSV. Naming a recipient on the export (`--recipient`) records the offline disclosure as a `DataDisclosed` provenance event — codes, DIDs and hashes only, never PII. All admin operations are audit-logged.

Approval enables a participant in three steps, in order: a **Keycloak user**, a **member in the REC registry**, then a **dataspace identity**. Registry registration fails closed — a participant missing from it is enabled in name only, invisible to every pipeline and dashboard that joins on `user_id`, POD and sensor ids. Which community they join, and their area, are per-community settings in the template manifest's `rec_registry:` block; without one, registration is skipped and the wizard still works.

When dataspace provisioning is enabled (`DATASPACE_ENABLED=true`), changing a submission to `approved` provisions a dataspace identity via the **identity-registry** HTTP API: a user DID, a Verifiable Credential, a membership in the REC organization, and a `dataspace_did` attribute on the Keycloak user. Onboarding keeps only the subject ID, DID, credential ID, and issuance timestamp. If `DS_CONNECTOR_URL` is set and the applicant gave data-sharing consent, the consented offers are then provisioned to the dataspace connector as a final, non-fatal step; a failed share leaves `share_provisioned=false` and can be retried via `POST /api/admin/submissions/{id}/retry-share`. See [Dataspace Integration](docs/dataspace-integration.md) and [Data Sharing](docs/data-sharing.md) for details.

### For the community

Each REC gets a template folder that customizes the platform without code changes:

- **Branding** — name, logo, primary color (applied as CSS variables site-wide)
- **Consent documents** — local PDFs or links to external URLs, with versioning
- **Coverage area** — municipalities, postal codes, or regions for eligibility checks
- **Wizard steps** — reorderable via the manifest (skip eligibility if no coverage restriction)
- **Content** — markdown files for the welcome page, consent intro, and success message
- **Notifications** — sender address, operator email list, optional storage backend (S3/Google Drive), optional webhook

Templates are imported into the database with `task import-templates`, and served per community at `/{rec}` — one deployment hosts several.

## Architecture

**Backend**: Python 3.12, FastAPI (async), SQLAlchemy 2 (async), PostgreSQL, Alembic migrations. Rate limiting via slowapi. PDF generation with fpdf2. Email via SMTP.

**Frontend**: SvelteKit 5, CSS custom properties for theming, sveltekit-i18n (Italian + English), marked for markdown rendering with DOMPurify sanitization. No CSS framework — design tokens from a shared design system.

**Extraction pipeline**: uploaded files are classified by magic bytes. Images are compressed to JPEG (max 1600px, quality 75) and sent to the OpenAI Vision API. PDFs are converted to text via markitdown. Both go into a single LLM call that returns structured JSON. The model is configurable via env var.

**Eligibility**: addresses are geocoded via Nominatim (OpenStreetMap). The reverse-geocoded municipality/postal code is checked against rules defined in the template manifest. The checker is a protocol — swap in a different implementation for polygon checks, external APIs, etc.

## Security

### Encryption at rest

All PII is encrypted using Fernet symmetric encryption (`ENCRYPTION_KEY`). This covers:

- Uploaded documents (utility bills, ID cards) encrypted on disk
- Database columns: `first_name`, `last_name`, `email`, `phone`, `fiscal_code`, `pod_code`, `consent_ip`
- JSON fields: `extracted_data`, `id_extracted_data` (OCR results), `raw_response` (LLM responses)

Encryption is mandatory by default. The app refuses to start without `ENCRYPTION_KEY` unless `REQUIRE_ENCRYPTION=false` (dev-only). Legacy unencrypted data is read gracefully during migration.

### Session and authentication

- **Applicant sessions**: 32-byte random tokens with 10-minute inactivity TTL. All data-mutating endpoints (including extraction) require a valid session token via `X-Session-Token` header.
- **Admin endpoints** (`/api/admin/**`): a Keycloak identity, verified against the issuer's JWKS (signature, issuer, audience, expiry). Authorised by the caller's **organization + group** for operators (`admins`/`managers`/`editors`/`viewers`, at realm or organization level) and by **scope** for service accounts, decided by OPA policies in `policies/`. Every action is audit-logged against the actor.
- **Download links**: Fernet-encrypted tokens with configurable TTL (default 24 hours).

### HTTP hardening

Security headers are enabled by default (`SECURITY_HEADERS=true`): X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy. CORS is configurable with restricted methods/headers. Rate limiting on extraction (10/hr), submission creation (20/hr), PDF download (5/min).

### GDPR

- Consent-first: data collection only after explicit GDPR and policy consent, with IP, timestamp, and document version recorded
- Right to erasure: `DELETE /api/admin/submissions/{id}` removes files from disk and all DB records
- Audit trail: all admin operations logged with action, entity, IP, and detail
- DPA enforcement: app refuses to start with LLM extraction steps unless `DPA_SIGNED=yes` (and SMS providers unless `DPA_SMS_SIGNED=yes`)
- CER field coverage vs GSE registration: see [docs/regulatory-compliance.md](docs/regulatory-compliance.md)
- Data minimization: `consent_ip` excluded from public API responses, only visible to admins
- Markdown content sanitized with DOMPurify to prevent XSS

## Quick Start

### Prerequisites

- PostgreSQL (external, already running)
- Python 3.12+ with [uv](https://docs.astral.sh/uv/)
- Node.js 22+ with pnpm
- [Task](https://taskfile.dev/) (optional, for task runner)

### Setup

```bash
# Clone and configure
cp .env.example .env
# Edit .env — required: DATABASE_URL, OPENAI_API_KEY, ENCRYPTION_KEY
# For dev without encryption: set REQUIRE_ENCRYPTION=false

# Backend
cd src && uv sync && cd ..

# Frontend
cd ui && pnpm install --ignore-scripts && cd ..

# Database
task migrate    # or: uv run --project src alembic upgrade head

# Run
task run:api    # FastAPI on :8000
task run:ui     # SvelteKit on :5173 (proxies /api to backend)
```

### Docker

```bash
docker compose up
```

This creates the database, runs migrations, and starts backend + frontend. Requires an external PostgreSQL instance (configured via `DB_HOST`, `DB_PORT`, etc.).

### Choosing a template

```bash
task import-templates -- --filter my-community
```

See `templates/example/` for the manifest format.

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL async connection string (e.g. `postgresql+asyncpg://user:pass@host:5432/db`) |
| `OPENAI_API_KEY` | OpenAI API key for bill/ID extraction |
| `ENCRYPTION_KEY` | Fernet key for PII encryption. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `DPA_SIGNED` | Set to `yes` after signing a DPA with your LLM provider (required when using extraction steps) |
| `OIDC_BASE_URL` | Keycloak realm issuer for the admin console and outbound M2M (e.g. `http://keycloak.celine.localhost/realms/celine`). Startup refuses without it. |

### Security

| Variable | Default | Description |
|---|---|---|
| `REQUIRE_ENCRYPTION` | `true` | App refuses to start without `ENCRYPTION_KEY`. Set `false` for local dev only. |
| `SECURITY_HEADERS` | `true` | Adds security headers to all responses. Disable if your reverse proxy handles them. |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:5173` | Comma-separated allowed origins |
| `DOWNLOAD_TOKEN_TTL` | `86400` | Download link expiry in seconds (default: 24 hours) |

### Application

| Variable | Default | Description |
|---|---|---|
| `TEMPLATES_DIR` | `./templates` | Root directory templates are imported from |
| `DATA_DIR` | `./data` | Upload and export storage path |
| `MAX_UPLOAD_SIZE_MB` | `10` | Maximum file upload size |
| `EXTRACTION_BASE_URL` | `https://api.openai.com/v1` | Base URL for OpenAI-compatible API |
| `EXTRACTION_MODEL` | `gpt-5.4` | Model for OCR extraction |

### Email (SMTP)

All optional. If `SMTP_HOST` is unset, email notifications are silently skipped.

| Variable | Default | Description |
|---|---|---|
| `SMTP_HOST` | *(none)* | SMTP server hostname |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USER` | *(none)* | SMTP username |
| `SMTP_PASSWORD` | *(none)* | SMTP password |
| `SMTP_FROM` | *(none)* | Sender address (overridden by manifest `notifications.from`) |
| `SMTP_TLS` | `true` | STARTTLS with certificate verification |
| `SMTP_NOTIFY` | *(none)* | Fallback operator emails (overridden by manifest `notifications.notify`) |

### Phone Verification (SMS OTP)

Optional. When a REC manifest's `steps` includes `phone_verify`, participants verify their phone via an SMS one-time code, and approval is gated on successful verification. Defaults to a `log` provider (prints the code) for local dev; any real provider requires a signed DPA. See [docs/phone-verification.md](docs/phone-verification.md).

| Variable | Default | Description |
|---|---|---|
| `SMS_PROVIDER` | `log` | `log` (dev) or `brevo` |
| `BREVO_API_KEY` | *(none)* | Required for `brevo` |
| `BREVO_SMS_SENDER` | *(none)* | Alphanumeric sender id or E.164; required for `brevo` |
| `SMS_OTP_TEMPLATE` | `Il tuo codice di verifica e' {code}` | Message body (must contain `{code}`) |
| `DPA_SMS_SIGNED` | `false` | Must be `yes` for any non-`log` provider (GDPR Art. 28) |
| `OTP_CODE_LENGTH` | `6` | Digits in the code |
| `OTP_TTL_SECONDS` | `600` | Code validity |
| `OTP_MAX_ATTEMPTS` | `3` | Wrong guesses before lockout |
| `OTP_MAX_SENDS_PER_HOUR` | `3` | Per phone number |
| `OTP_LOCKOUT_SECONDS` | `3600` | Lockout duration |

### Dataspace Identity Provisioning

Optional. Set `DATASPACE_ENABLED=true` to provision a dataspace identity (DID + Verifiable Credential + REC organization membership + Keycloak DID attribute) when an admin approves a submission. Requires the **identity-registry** service and `celine-sdk>=1.13.0` for M2M authentication.

Which organization a community's members join is set **per community**, in that template's `manifest.yaml` under `dataspace:` — not as a deployment-wide variable. Omit the block and the community simply is not in the dataspace. The organization must already exist and be promoted in the registry; onboarding never creates one. See [docs/dataspace-integration.md](docs/dataspace-integration.md) for the full integration guide.

After approval a participant manages and withdraws their sharing decisions in the dataspace portal (`/my-data`), authenticated by their own credential — not here.

| Variable | Default | Description |
|---|---|---|
| `DATASPACE_ENABLED` | `false` | Deployment-wide gate for dataspace identity provisioning. A community also needs a `dataspace:` block in its manifest |
| `IDENTITY_REGISTRY_URL` | *(none)* | Base URL of the identity-registry service |
| `OIDC_BASE_URL` | *(none)* | OIDC issuer URL for M2M token acquisition |
| `DS_ONBOARDING_CLIENT_ID` | `svc-ds-onboarding` | Keycloak client ID for M2M auth |
| `DS_ONBOARDING_CLIENT_SECRET` | *(none)* | Keycloak client secret for M2M auth |
| `DATASPACE_USER_ROLE` | *(none)* | Role assigned in the credential |
| `DATASPACE_ALLOWED_ACTIONS` | *(none)* | Comma-separated authorized actions |
| `DATASPACE_VC_TTL_DAYS` | *(none)* | Credential validity period in days |
| `DATASPACE_SUBJECT_SOURCE` | `email_hash` | Subject ID source (`email_hash` delegates derivation to the identity-registry's `GET /users/resolve?derive=true`) |

Which organization a community's members join, its DID and the linked participant are **per community**, in that template's `manifest.yaml` under `dataspace:` — there is no deployment-wide equivalent, because one would file every community's members into a single organization.
| `DS_CONNECTOR_URL` | *(none)* | Connector base URL for provisioning data-sharing consent on approval (`POST /consent/admin/shares`). Empty disables share provisioning |
| `DS_NS_URL` | *(none)* | Public vocabulary base (`GET /ns/sharing-offers`) the wizard renders offers from; empty falls back to the connector's `/ns` path |
| `DS_PROVENANCE_URL` | *(none)* | Provenance base URL for recording a named-recipient CSV export as a `DataDisclosed` event (`POST /prov/events`, scope `provenance.write`); empty disables the emission |

## Creating a Template

```
templates/my-rec/
  manifest.yaml          # community config
  assets/logo.svg        # branding
  consent/               # local consent docs (optional if using URLs)
    policy.pdf
    policy.pdf.json      # metadata sidecar: slug, title, version, mime_type
  content/
    welcome.md           # landing page body
    consent_intro.md     # shown above consent checkboxes
    success.md           # shown after submission
```

The manifest declares everything the platform needs to customize for this community: name, branding, consent document versions and locations, coverage rules, wizard step order, notification recipients, optional storage backend, and optional webhook. See `AGENTS.md` for the full manifest schema.

## Development

```bash
task run:api              # backend with hot reload
task run:ui               # frontend with hot reload + API proxy
task migrate              # apply migrations
task migration -- "msg"   # create new migration
task test                 # backend + frontend tests
task lint                 # ruff + svelte-check
task sdk:local            # dev-only: celine-sdk from ../celine-sdk (unreleased wrappers)
task export-csv           # export submissions to data/exports/
task export-pod-list      # export consented supply points for a distributor
```

### Adding a field

1. Add the column to `src/celine/onboarding/models/submission.py` — use `EncryptedString` for PII fields
2. Add to `SubmissionUpdate` and `SubmissionRead` in `models/schemas.py` — add to `SubmissionAdminRead` if it should be admin-only
3. Run `task migration -- "add_field_name"` then `task migrate`
4. Add the form field in `ui/src/routes/onboarding/+page.svelte`
5. Add i18n keys in `ui/src/lib/i18n/{it,en}/onboarding.json`

### Adding a wizard step

1. Add the step name to the template's `manifest.yaml` `steps` list
2. Add a label mapping in `STEP_LABELS` in the wizard page
3. Add a `{:else if currentStepName === 'mystep'}` block in the template
4. Add `canProceed` logic for the step
5. Add any `advanceStep` save logic

### Adding a coverage rule type

1. Add the field name to `RULE_FIELD_MAP` in `services/eligibility.py`
2. Parse the field from Nominatim's address response in `_parse_address`
3. Use it in the manifest: `{ type: "my_field", values: [...] }`

## License

Copyright 2026 Spindox Labs

Apache-2.0 see LICENSE
