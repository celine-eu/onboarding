# REC Onboarding Platform

A self-hosted web application for onboarding members into Renewable Energy Communities (REC). Built for Italian CERs (Comunita Energetiche Rinnovabili), designed to work across EU regions.

## Why

Joining a Renewable Energy Community involves collecting personal data, verifying utility contracts, and obtaining legal consents. Most RECs do this via paper forms, email exchanges, and manual data entry — error-prone and hard to scale.

This platform automates the process: a public-facing wizard collects data from applicants, extracts details from utility bills using AI, validates fields, checks geographic eligibility, and delivers a complete submission to the operator — with a full GDPR audit trail.

## How It Works

### For the applicant

1. **Accept consents** — GDPR privacy policy and community rules, with links to the actual documents. This step creates the submission and records the IP address, timestamp, and document versions.
2. **Upload utility bill** (optional, and only where [document scanning](#document-upload-and-scanning) is enabled) — photos or PDFs of the electricity bill. The system uses AI vision to extract the holder's name, fiscal code, POD code, address, and provider. Multiple pages can be uploaded; each one refines the extracted data.
3. **Confirm personal data** — a form, pre-filled with extracted data when there is some. The applicant reviews and corrects. Fiscal code and POD are validated against their official formats. Where scanning is enabled, an optional ID card upload provides cross-validation against bill data; where it is not, the applicant types these fields and nothing is uploaded.
4. **Eligibility check** (if configured) — the applicant's address is geocoded and checked against the community's coverage area (municipalities, postal codes, or regions).
5. **Accept statute** — the community's founding document, presented separately from the data-collection consents. If the community enables it, this step also offers an **optional data-sharing consent**: the applicant can authorise sharing specific offers into the dataspace. It is never required and does not block submission (GDPR Art. 7(4)).
6. **Review and submit** — summary of all entered data. On submit, the applicant receives a PDF summary and the operator is notified by email.

The entire process has a 10-minute inactivity window. After that, the session token expires and the public API rejects further requests. This limits the exposure window for personal data.

### For the operator

Operators work in the console at `/admin`, signing in with their Keycloak identity; what they may do is decided by their organization and group (see [Authorization](docs/authorization.md)). They can work the queue, open a submission in full — consents, documents, extracted data, enablement — change status, repair a failed enablement step, and export to CSV. The same flow is available from the terminal with `onboarding-cli admin`; see [Operator console](docs/admin-console.md). Naming a recipient on the export (`--recipient`) records the offline disclosure as a `DataDisclosed` provenance event — codes, DIDs and hashes only, never PII. All admin operations are audit-logged.

Approval enables a participant in three steps, in order: a **login**, a **member in the REC registry**, then a **dataspace identity**. The login is provisioned by asking `celine-policies`' provisioning service (`PROVISIONING_URL`) — this service holds no Keycloak grant of its own; see [ADR-0004](docs/decisions/ADR-0004-ask-the-provisioning-service-instead-of-administering-the-realm.md). Approval also asks for an invitation: Keycloak emails the participant a link to set their password, valid for 7 days, in the language they last used in the wizard. Whether it is sent is the provisioning service's decision (not for an account that already has a password, nor for a disabled one, nor twice within a short per-account cooldown), and the outcome is shown on the step in the console and by `onboarding-cli admin enablement status`; see [Operator console](docs/admin-console.md#what-approval-actually-does). A community manager can also send any registry member an invitation or a password reset from the `celine-community` dashboard. That call goes through this service's member-keyed admin routes, because this service is the provisioning service's only caller; see [API reference](docs/api-reference.md) and [ADR-0005](docs/decisions/ADR-0005-onboarding-is-the-one-caller-of-the-provisioning-service.md). Without `PROVISIONING_URL`, or for a community with no `rec_registry:` block, that step is skipped and the participant is onboarded without a login, or an invitation, and the wizard does not promise one. Registry registration fails closed — a participant missing from it is enabled in name only, invisible to every pipeline and dashboard that joins on `user_id`, POD and sensor ids. Which community they join, and their area, are per-community settings in the template manifest's `rec_registry:` block; without one, registration is skipped and the wizard still works.

When dataspace provisioning is enabled (`DATASPACE_ENABLED=true`), changing a submission to `approved` provisions a dataspace identity via the **identity-registry** HTTP API: a user DID, a Verifiable Credential, a membership in the REC organization, and a `dataspace_did` attribute on the Keycloak user. Onboarding keeps only the subject ID, DID, credential ID, and issuance timestamp. If `DS_CONNECTOR_URL` is set and the applicant gave data-sharing consent, the consented offers are then provisioned to the dataspace connector as a final, non-fatal step; a failed share leaves `share_provisioned=false` and can be retried from the console or via `POST /api/admin/{rec}/submissions/{id}/enablement/retry`. See [Dataspace Integration](docs/dataspace-integration.md) and [Data Sharing](docs/data-sharing.md) for details.

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

**Frontend**: SvelteKit 5, CSS custom properties for theming, sveltekit-i18n (Italian, English, Spanish — wizard and operator console), marked for markdown rendering with DOMPurify sanitization. No CSS framework — design tokens from a shared design system.

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
- **Admin endpoints** (`/api/admin/**`): a Keycloak identity, verified against the issuer's JWKS (signature, issuer, audience, expiry). Authorised by the caller's **organization + group** for operators (`admins`/`managers`/`editors`/`viewers` inside a Keycloak organization typed `rec`; only `admins`/`managers` grant anything at realm level, where a badge is platform-wide) and by **scope** for service accounts, decided by OPA policies in `policies/`. Every action is audit-logged against the actor.
- **Download links**: Fernet-encrypted tokens with configurable TTL (default 24 hours).

### HTTP hardening

Security headers are enabled by default (`SECURITY_HEADERS=true`): X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy. CORS is configurable with restricted methods/headers. Rate limiting on extraction (10/hr), submission creation (20/hr), PDF download (5/min).

### GDPR

- Consent-first: data collection only after explicit GDPR and policy consent, with IP, timestamp, and document version recorded
- Right to erasure: `DELETE /api/admin/{rec}/submissions/{id}` removes files from disk and all DB records
- Audit trail: all admin operations logged with action, entity, IP, detail **and the operator who performed them**
- Processing agreements: bill and ID scanning send identity documents to the extraction provider, so document upload and scanning are **off** unless `EXTRACTION_ENABLED=yes` and `EXTRACTION_API_KEY` are both set — the wizard then collects personal data without documents and the document endpoints answer 403 (see [Document upload and scanning](#document-upload-and-scanning)). Phone verification follows the same pattern: a real SMS provider without `DPA_SMS_SIGNED=yes` starts with verification **off** (see [Phone Verification](#phone-verification-sms-otp))
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
# Clone and configure. On the celine-dev workspace you need one setting:
cp .env.example .env
# Edit .env — set ENCRYPTION_KEY (or REQUIRE_ENCRYPTION=false for dev).
# Every address already defaults to a value that resolves to the same service
# whether the process runs on the host or in a container, so DATABASE_URL and
# OIDC_BASE_URL need no entry here. Off this workspace, set them.

# Optional: .env.local for anything true on your machine only — your own
# service URLs, dev secrets. It is read after .env and wins, and it is
# gitignored, so it never reaches a deployment. Real environment variables
# still win over both.

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

### How the defaults are chosen

An address only gets a default if **one string resolves to the same service from
the host and from inside a container** — otherwise the checkout ends up holding
two contradictory sets of them, which is what it held until 2026-09-12.

`172.17.0.1` is the docker bridge gateway: the host as seen from a container, a
local interface as seen from the host. It serves any service published on a host
port, and it is the workspace convention. Where the string is *compared* rather
than dialled the default is a hostname instead — `OIDC_BASE_URL` is checked
against the `iss` claim, and Keycloak mints `iss` from its own hostname whatever
address the caller used, so `172.17.0.1:8080` reaches the right realm and reports
the wrong issuer.

An address whose emptiness *disables* a dependency keeps no default, because the
address is the only thing that says whether the dependency is there:
`PROVISIONING_URL`, `REC_REGISTRY_URL`, `DS_CONNECTOR_URL`, `DS_NS_URL`,
`DS_PROVENANCE_URL`, `IDENTITY_REGISTRY_URL`, `DATASPACE_KEYCLOAK_REALM`.

### Required

| Variable | Description |
|---|---|
| `ENCRYPTION_KEY` | Fernet key for PII encryption. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. The one thing you must set; `REQUIRE_ENCRYPTION=false` skips it in development only |

### Defaulted, but wrong off the celine-dev workspace

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:securepassword123@172.17.0.1:15432/rec_onboarding` | The workspace's shared host Postgres, which is also where `docker compose up` puts the database |
| `OIDC_BASE_URL` | `http://keycloak.celine.localhost/realms/celine` | Keycloak realm issuer for the admin console and outbound M2M. **The one default that is silently wrong rather than merely absent**: elsewhere its JWKS is unreachable and every `/api/admin` request is denied, so the app logs a warning at boot while it is in force |
| `ONBOARDING_API_URL` | `http://172.17.0.1:8040` | What `onboarding-cli` talks to |

### Not defaulted, on purpose

| Variable | Description |
|---|---|
| `PROVISIONING_URL` | Internal address of `celine-policies`' provisioning service, which provisions participant logins (e.g. `http://provisioning:8010`). Empty onboards participants without a login. It must have no public route — which is also why it has no both-sides address to default to |

### Document upload and scanning

Off by default. Scanning sends a participant's utility bill and identity document to the extraction endpoint at `EXTRACTION_BASE_URL`, any OpenAI-compatible API. Unless the deployment's own operator runs that endpoint, its provider is a processor under GDPR Art. 28. Both variables must be set to switch it on; with either missing the app still starts, logs one warning naming what is missing, and runs without the feature:

- the wizard offers no upload on any step and drops a `utility` step, so the participant types their personal data;
- `POST /api/{rec}/extract`, `/extract-id`, `/documents/{id}/extract`, `/extractions/{id}/confirm`, and a `utility_bill` or `id_card` upload to `/submissions/{id}/documents`, answer **403** with `detail.code` `document_processing_disabled`;
- `GET /api/{rec}/config` reports `features.document_upload` and `features.document_scan`, which is what the wizard reads.

| Variable | Default | Description |
|---|---|---|
| `EXTRACTION_ENABLED` | `false` | Set to `yes` only once the endpoint at `EXTRACTION_BASE_URL` is operated by this deployment's own operator, or covered by a processing agreement that keeps processing in the EU |
| `EXTRACTION_API_KEY` | *(none)* | Key for the endpoint at `EXTRACTION_BASE_URL`. An in-house server is started with a key too (for vLLM, `--api-key`) |

`DPA_SIGNED` and `OPENAI_API_KEY` were renamed to these on 2026-09-14 and are no longer read. A deployment still setting them starts with scanning off and logs the rename.

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

The defaults are for development: they point at the Mailpit that `celine-policies`' compose publishes on the host's port 1025 (UI on 8025), the same inbox Keycloak's invitation emails land in, so nothing reaches a real person. A deployment overrides them with its relay; `SMTP_HOST=` (set, empty) switches email off. The app logs a warning at boot while the development host is in force.

| Variable | Default | Description |
|---|---|---|
| `SMTP_HOST` | `172.17.0.1` (dev Mailpit) | SMTP server hostname; empty disables email |
| `SMTP_PORT` | `1025` | SMTP port |
| `SMTP_USER` | *(none)* | SMTP username |
| `SMTP_PASSWORD` | *(none)* | SMTP password |
| `SMTP_FROM` | `onboarding@celine.localhost` | Sender address (overridden by manifest `notifications.from`) |
| `SMTP_TLS` | on, except for the dev host | STARTTLS with certificate verification. Unset follows `SMTP_HOST`, so a relay gets TLS without asking |
| `SMTP_NOTIFY` | *(none)* | Fallback operator emails (overridden by manifest `notifications.notify`) |

### Phone Verification (SMS OTP)

Optional. When a REC manifest's `steps` includes `phone_verify`, participants verify their phone via an SMS one-time code, and approval is gated on successful verification. Defaults to a `log` provider (prints the code) for local dev.

A real provider receives participants' phone numbers, so it is used only with `DPA_SMS_SIGNED=yes`. Without it — or with an unknown `SMS_PROVIDER` — the app still starts and logs one warning, and phone verification is **off**: the wizard leaves the `phone_verify` step out, `verify-phone` and `confirm-phone` answer 403 with `detail.code` `phone_verification_disabled`, `GET /api/{rec}/config` reports `features.phone_verification: false`, and approval does not wait for a verification that cannot happen. The console shows that on the submission and the approval's audit row records the waiver. See [docs/phone-verification.md](docs/phone-verification.md).

| Variable | Default | Description |
|---|---|---|
| `SMS_PROVIDER` | `log` | `log` (dev) or `brevo` |
| `BREVO_API_KEY` | *(none)* | Required for `brevo` |
| `BREVO_SMS_SENDER` | *(none)* | Alphanumeric sender id or E.164; required for `brevo` |
| `SMS_OTP_TEMPLATE` | `Il tuo codice di verifica e' {code}` | Message body (must contain `{code}`) |
| `DPA_SMS_SIGNED` | `false` | Set to `yes` only once a Data Processing Agreement with the SMS provider is signed (GDPR Art. 28). Without it a real provider leaves phone verification off |
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
| `OIDC_BASE_URL` | `http://keycloak.celine.localhost/realms/celine` | OIDC issuer URL for M2M token acquisition — the same issuer the admin console verifies inbound tokens against |
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
task export-csv           # export submissions to data/exports/
task export-pod-list      # export consented supply points for a distributor
```

### Adding a field

1. Add the column to `src/celine/onboarding/models/submission.py` — use `EncryptedString` for PII fields
2. Add to `SubmissionUpdate` and `SubmissionRead` in `models/schemas.py` — add to `SubmissionAdminRead` if it should be admin-only
3. Run `task migration -- "add_field_name"` then `task migrate`
4. Add the form field in `ui/src/routes/onboarding/+page.svelte`
5. Add i18n keys in `ui/src/lib/i18n/{it,en,es}/onboarding.json`

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
