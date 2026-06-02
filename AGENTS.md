# CER Onboarding Platform — Operational Setup

## Objectives

- Web platform for onboarding participants into Italian CERs (Comunita Energetiche Rinnovabili)
- Generic onboarding/workflow platform for REC, designed to scale to other EU regions
- Simplify data/document collection, reduce manual entry
- OCR/LLM extraction from utility bills to pre-fill forms (user confirms before submit)
- Configurable workflow with pluggable extractors, validators, policies, outputs
- Privacy/legal compliance (GDPR consents with IP, timestamps, document versions, audit trail)

## Tech Stack

### Backend (`./src`)

- **Package:** `celine.onboarding` (namespace package — `celine/` has no `__init__.py`)
- **Framework:** FastAPI, async
- **Models/config:** Pydantic, Pydantic Settings (`.env` driven)
- **Database:** PostgreSQL (`rec_onboarding`), Alembic migrations at repo root (`./alembic/`)
- **CLI:** Typer (CSV export, ops commands)
- **Extraction:** OpenAI Vision API (GPT-4o), base URL overridable via env
- **Validators:** Italian codice fiscale (checksum), POD code format
- **Package management:** uv + pyproject.toml + hatchling build

### Frontend (`./ui`)

- **Framework:** SvelteKit 5 (runes syntax)
- **Styling:** CELINE design system — CSS custom properties (`--celine-*`), DM Sans font, no Tailwind
- **Package management:** pnpm with `ignore-scripts=true` (supply chain security)
- **i18n:** sveltekit-i18n, Italian (default) + English
- **Components:** Reusable, isolated — designed for embedding in other webapps
- **Testing:** End-to-end with Playwright
- **UX:** Mobile-first, wizard-style onboarding flow, camera/upload support
- **API proxy:** Vite dev server proxies `/api` to backend

### Infrastructure

- **`./Dockerfile`** — backend container (Python 3.12, uv)
- **`./ui/Dockerfile`** — frontend container (Node 22, pnpm)
- **`./docker-compose.yml`** — init-db + migrate + backend + frontend (external Postgres)
- **`./Taskfile.yaml`** — common ops (run:api, run:ui, migrate, test, lint, export)
- `docker compose up` creates the database, runs migrations, and starts services

## Project Layout

```
./
├── src/                        # Python backend
│   ├── pyproject.toml
│   └── celine/onboarding/      # celine = namespace pkg (no __init__.py)
│       ├── main.py             # FastAPI app factory
│       ├── api/                # FastAPI routes
│       │   ├── health.py
│       │   ├── submissions.py
│       │   ├── documents.py
│       │   ├── extractions.py
│       │   └── consent_documents.py
│       ├── models/             # SQLAlchemy + Pydantic schemas
│       │   ├── database.py
│       │   ├── submission.py
│       │   ├── document.py
│       │   ├── extraction.py
│       │   └── schemas.py
│       ├── services/           # Business logic
│       ├── workflows/          # Status state machine
│       ├── extractors/         # OCR/LLM (OpenAI Vision)
│       ├── validators/         # CF checksum, POD format
│       ├── outputs/            # CSV export, Google Drive
│       ├── cli/                # Typer CLI
│       └── config/             # Pydantic Settings
├── alembic/                    # Database migrations (repo root)
│   ├── env.py
│   └── versions/
├── alembic.ini
├── ui/                         # SvelteKit frontend
│   ├── Dockerfile
│   ├── .npmrc                  # ignore-scripts=true
│   ├── src/
│   │   ├── app.css             # CELINE design tokens (--celine-*)
│   │   ├── lib/
│   │   │   ├── api/client.ts   # Typed API client
│   │   │   ├── components/     # FormField, FileUpload, ConsentCheckbox, ExtractionReview
│   │   │   └── i18n/           # it/, en/ translation JSON files
│   │   └── routes/
│   │       ├── +page.svelte    # Landing page
│   │       └── onboarding/     # Wizard flow
│   ├── tests/                  # Playwright e2e tests
│   └── playwright.config.ts
├── data/                       # All user data (gitignored)
│   ├── consent/                # Consent document PDFs + metadata sidecars
│   │   ├── gdpr.pdf + gdpr.pdf.json
│   │   ├── policy.pdf + policy.pdf.json
│   │   └── statute.pdf + statute.pdf.json
│   ├── submissions/            # Uploaded files per submission
│   │   └── <YYYYMMDD-shortid>/ # Sortable ref as folder name
│   └── exports/                # CSV exports (default output)
├── Dockerfile
├── docker-compose.yml
├── Taskfile.yaml
├── .env.example
└── .gitignore
```

## Data Directory (`./data`)

All user data lives under `DATA_DIR` (default `./data`, configurable via env).

| Path | Contents |
|---|---|
| `data/consent/` | Consent document PDFs + `.json` metadata sidecars |
| `data/submissions/<ref>/` | Uploaded documents per submission (ref = `YYYYMMDD-shortid`) |
| `data/exports/` | CSV exports |

## Configuration

All config via environment variables, loaded through Pydantic `BaseSettings`.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...@172.17.0.1:15432/rec_onboarding` | External Postgres |
| `OPENAI_API_KEY` | *(none)* | **Required** for bill extraction |
| `EXTRACTION_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint |
| `DATA_DIR` | `./data` (repo root) | All uploads, exports, consent docs |
| `MAX_UPLOAD_SIZE_MB` | `10` | Max file upload size |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` | docker-compose defaults | Used by init-db and migrate containers |

## Onboarding Wizard Flow

```
Step 1: Consents         → GDPR + policy (with PDF preview links) + keep-me-updated opt-in
                            Creates submission (UUID + sortable ref), records IP + timestamps + doc versions
Step 2: Bill Upload      → Optional. Upload utility bill photo/PDF
                            Auto-triggers OpenAI extraction → user reviews/confirms extracted data
Step 3: Personal Data    → Name, email, phone, CF, POD — pre-filled from extraction if available
Step 4: Statute          → Statute consent (separate from data collection consents)
Step 5: Review & Submit  → Summary of all data, final submit
```

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Liveness check |
| `POST` | `/api/submissions` | Create submission (consent-first, records IP) |
| `GET` | `/api/submissions` | List submissions (paginated) |
| `GET` | `/api/submissions/{id}` | Get submission detail |
| `PATCH` | `/api/submissions/{id}` | Update fields or status |
| `POST` | `/api/submissions/{id}/documents` | Upload document |
| `GET` | `/api/submissions/{id}/documents` | List documents |
| `GET` | `/api/documents/{id}/download` | Download/serve file |
| `POST` | `/api/extract` | Stateless bill extraction (no submission required) |
| `POST` | `/api/documents/{id}/extract` | Extract from stored document |
| `POST` | `/api/extractions/{id}/confirm` | Confirm extracted data |
| `GET` | `/api/consent-documents` | List consent docs with metadata |
| `GET` | `/api/consent-documents/{slug}` | Download/preview consent PDF |
| `GET` | `/api/consent-documents/{slug}/meta` | Consent doc metadata |

## Submission Model

Each submission has:
- `id` (UUID) — database primary key
- `ref` (`YYYYMMDD-shortid`) — sortable human-readable reference, used as folder name
- `status` — draft → submitted → under_review → approved / rejected
- Personal data: first_name, last_name, email, phone, fiscal_code, pod_code
- Consent audit trail: consent_ip, gdpr/policy/statute consent + timestamp + document version
- `keep_me_updated` — optional marketing opt-in

## Docker Compose

Uses an **external Postgres** (no bundled postgres service). Init containers handle setup:

1. `init-db` — creates `rec_onboarding` database if it doesn't exist
2. `migrate` — runs `alembic upgrade head`
3. `backend` — starts after migration completes
4. `frontend` — SvelteKit app

## Local Development

```bash
# Start everything (Docker)
docker compose up

# Or run locally (requires Postgres)
task run:api          # FastAPI on :8000
task run:ui           # SvelteKit on :5173 (proxies /api to backend)

# Database
task migrate          # alembic upgrade head
task migration -- "add_field"  # create new migration

# Quality
task test             # backend + frontend tests
task lint             # ruff + svelte-check

# Export
task export-csv       # CSV to ./data/exports/
```

## MVP Scope

Build:
- Public onboarding wizard (SvelteKit, i18n IT/EN)
- Consent-first flow with audit trail (IP, timestamps, document versions)
- Document upload + OCR/LLM extraction (OpenAI Vision)
- PostgreSQL storage with Alembic migrations
- Manual review workflow (status state machine)
- Typer CLI for CSV export
- Consent document preview/download endpoint
- Docker Compose with init-db + migrate containers
- Playwright e2e tests

Do not build (yet):
- Google Sheets output
- Auth/IAM
- Regional policies
- Event system
- ERP/CRM/GSE integrations
- Microservices / Kubernetes
