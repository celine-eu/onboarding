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
- **`./Taskfile.yaml`** — run:api, run:ui, migrate, test, lint, export-csv

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
│   │   ├── consent_documents.py # PDF/URL serving
│   │   ├── config.py           # Template config + assets
│   │   ├── downloads.py        # Token-authenticated document download
│   │   └── deps.py             # Shared: limiter, admin auth, session auth
│   ├── models/                 # SQLAlchemy + Pydantic schemas
│   │   ├── encrypted.py        # EncryptedString + EncryptedJSON TypeDecorators
│   │   └── ...
│   ├── services/               # Business logic, email, PDF, templates, eligibility
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
| `TEMPLATE_DIR` | `./templates/example` | Active community template. |
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

Selected via `TEMPLATE_DIR` env var.

## Wizard Flow

1. **Consents** — GDPR + policy + keep-me-updated. Creates the submission (UUID, IP, timestamps).
2. **Bill Upload** — Optional multi-page upload. AI extraction produces editable prefilled data.
3. **Personal Data** — Name, email, phone, CF, POD (validated, prefilled from extraction) + manifest extra fields. Optional ID card upload with cross-validation against bill data.
4. **Energy System** — PV, battery, EV, heat pump questions (manifest-driven, with conditional visibility).
5. **Eligibility** — Address geocoded and checked against coverage rules (if configured).
6. **Statute** — Separate consent for community statute.
7. **Review** — Summary of all entered data. Submit triggers PDF generation + email notification.

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
| `POST` | `/api/eligibility` | none | Coverage check |
| `GET` | `/api/config` | none | Template config |
| `GET` | `/api/consent-documents/{slug}` | none | PDF or redirect |
| `GET` | `/api/downloads/{token}` | token | Time-limited document download |

**Admin (token-protected, no TTL, audit-logged):**

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/admin/submissions` | List all (includes consent_ip) |
| `GET/PATCH` | `/api/admin/submissions/{id}` | View/update (includes consent_ip) |
| `DELETE` | `/api/admin/submissions/{id}` | GDPR erasure (files + DB) |
| `GET` | `/api/admin/submissions/{id}/pdf` | Download summary |
| `GET` | `/api/admin/audit-logs` | Paginated audit trail |

## Local Development

```bash
task run:api          # FastAPI on :8000
task run:ui           # SvelteKit on :5173 (proxies /api)
task migrate          # alembic upgrade head
task migration -- "description"
task test             # backend + frontend
task lint             # ruff + svelte-check
task export-csv       # CSV to ./data/exports/
```
