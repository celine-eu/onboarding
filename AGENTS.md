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
- **Rendering:** marked for markdown content from templates
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
│   ├── main.py                 # App factory, CORS, rate limiting
│   ├── api/
│   │   ├── admin.py            # Token-protected operator endpoints
│   │   ├── submissions.py      # Public endpoints (10min TTL)
│   │   ├── documents.py        # Upload/list (TTL-gated)
│   │   ├── extractions.py      # OCR extraction (rate-limited)
│   │   ├── eligibility.py      # Coverage check (geocoding)
│   │   ├── consent_documents.py # PDF/URL serving
│   │   ├── config.py           # Template config + assets
│   │   └── deps.py             # Shared: limiter, admin auth
│   ├── models/                 # SQLAlchemy + Pydantic schemas
│   ├── services/               # Business logic, email, PDF, templates, eligibility
│   ├── extractors/             # OpenAI Vision + markitdown
│   ├── validators/             # CF checksum, POD format
│   ├── workflows/              # Status state machine
│   ├── outputs/                # CSV export
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

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | *(local default)* | Async PostgreSQL connection |
| `OPENAI_API_KEY` | *(none)* | **Required** for extraction |
| `EXTRACTION_MODEL` | `gpt-5.4` | OpenAI model for OCR |
| `ENCRYPTION_KEY` | *(none)* | Fernet key for PII encryption at rest (generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) |
| `DPA_SIGNED` | `false` | **Required** (`yes`) when manifest uses LLM extraction (utility/identity steps). Sign DPA with your LLM provider first. |
| `ADMIN_TOKEN` | *(none)* | **Required** for `/api/admin/*` |
| `CORS_ORIGINS` | `localhost:3000,5173` | Comma-separated origins |
| `DATA_DIR` | `./data` | Uploads, exports |
| `TEMPLATE_DIR` | `./templates/example` | Active community template |
| `SMTP_HOST/PORT/USER/PASSWORD` | *(none)* | Email on submission |

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
content:
  welcome: content/welcome.md
  consent_intro: content/consent_intro.md
  success: content/success.md
```

Selected via `TEMPLATE_DIR` env var.

## Wizard Flow

```
Consents       → GDPR + policy + keep-me-updated → creates submission (UUID, IP, timestamps)
Bill Upload    → Optional multi-page upload → AI extraction → editable prefilled data
Personal Data  → Name, email, phone, CF, POD (validated, prefilled from extraction) + manifest extra fields
Energy System  → PV, battery, EV, heat pump questions (manifest-driven, with conditional visibility)
Eligibility    → Address geocoded → checked against coverage rules (if configured)
Statute        → Separate consent for community statute
Review         → Summary → submit → PDF download + email notification
```

## Security Model

- **Encryption at rest (files)**: uploaded documents encrypted with Fernet before writing to disk (`ENCRYPTION_KEY` env var). Decrypted transparently on read. Without a key, files are stored as plaintext (dev mode).
- **Column-level encryption (DB)**: `email`, `phone`, `fiscal_code`, `pod_code` encrypted at the application layer via `EncryptedString` SQLAlchemy TypeDecorator. Uses the same `ENCRYPTION_KEY`. Backwards-compatible: reads unencrypted legacy data gracefully.
- **Email hardening**: notification emails contain only the submission ref (no PII in body), no document ZIP attachment, STARTTLS enforced with certificate verification via `ssl.create_default_context()`.
- **Audit logging**: all admin operations (list, view, update, delete, PDF download) logged to `audit_logs` table with action, entity, IP, and detail. Viewable via `GET /api/admin/audit-logs`.
- **GDPR erasure**: `DELETE /api/admin/submissions/{id}` deletes files from disk, removes all DB records (CASCADE), and logs the deletion.
- **10-minute session TTL**: public endpoints return 410 after 10min from submission creation
- **Admin endpoints** (`/api/admin/*`): require `Authorization: Bearer <ADMIN_TOKEN>`
- **Rate limiting**: `/api/extract` 10/hr, `/api/submissions` POST 20/hr, PDF 5/min
- **Path traversal protection**: resolved paths validated against root directories
- **MIME validation**: magic bytes, not client headers
- **Input validation**: CF checksum + POD format enforced in Pydantic schemas
- **CORS**: configurable origins, restricted methods/headers
- **No public list endpoint**: submission enumeration only via admin token

## API Summary

**Public (TTL-gated, rate-limited):**

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/submissions` | Create (consent-first) |
| `GET/PATCH` | `/api/submissions/{id}` | Read/update own (10min) |
| `POST` | `/api/submissions/{id}/documents` | Upload (10min) |
| `GET` | `/api/submissions/{id}/pdf` | Download summary (10min) |
| `POST` | `/api/extract` | Bill OCR (10/hr) |
| `POST` | `/api/extract-id` | ID card OCR (10/hr) |
| `POST` | `/api/eligibility` | Coverage check |
| `GET` | `/api/config` | Template config |
| `GET` | `/api/consent-documents/{slug}` | PDF or redirect |

**Admin (token-protected, no TTL, audit-logged):**

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/admin/submissions` | List all |
| `GET/PATCH` | `/api/admin/submissions/{id}` | View/update |
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
