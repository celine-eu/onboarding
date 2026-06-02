# REC Onboarding Platform

A self-hosted web application for onboarding members into Renewable Energy Communities (REC). Built for Italian CERs (Comunita Energetiche Rinnovabili), designed to work across EU regions.

## Why

Joining a Renewable Energy Community involves collecting personal data, verifying utility contracts, and obtaining legal consents. Most RECs do this via paper forms, email exchanges, and manual data entry — error-prone and hard to scale.

This platform automates the process: a public-facing wizard collects data from applicants, extracts details from utility bills using AI, validates fields, checks geographic eligibility, and delivers a complete submission to the operator — with a full GDPR audit trail.

## How It Works

### For the applicant

1. **Accept consents** — GDPR privacy policy and community rules, with links to the actual documents. This step creates the submission and records the IP address, timestamp, and document versions.
2. **Upload utility bill** (optional) — photos or PDFs of the electricity bill. The system uses AI vision to extract the holder's name, fiscal code, POD code, address, and provider. Multiple pages can be uploaded; each one refines the extracted data.
3. **Confirm personal data** — a form pre-filled with extracted data. The applicant reviews and corrects. Fiscal code and POD are validated against their official formats.
4. **Eligibility check** (if configured) — the applicant's address is geocoded and checked against the community's coverage area (municipalities, postal codes, or regions).
5. **Accept statute** — the community's founding document, presented separately from the data-collection consents.
6. **Review and submit** — summary of all entered data. On submit, the applicant receives a PDF summary and the operator is notified by email.

The entire process has a 10-minute window. After that, the submission UUID expires and the public API rejects further requests. This limits the exposure window for personal data.

### For the operator

Operators access submissions via token-protected admin endpoints. They can list all submissions, review details, download PDF summaries, change status (submitted, under review, approved, rejected), and export to CSV.

### For the community

Each REC gets a template folder that customizes the platform without code changes:

- **Branding** — name, logo, primary color (applied as CSS variables site-wide)
- **Consent documents** — local PDFs or links to external URLs, with versioning
- **Coverage area** — municipalities, postal codes, or regions for eligibility checks
- **Wizard steps** — reorderable via the manifest (skip eligibility if no coverage restriction)
- **Content** — markdown files for the welcome page, consent intro, and success message
- **Notifications** — sender address and operator email list

The template is selected at deploy time via the `TEMPLATE_DIR` environment variable. One deployment per community for now; the architecture supports multi-tenant via template resolution.

## Architecture

**Backend**: Python 3.12, FastAPI (async), SQLAlchemy 2 (async), PostgreSQL, Alembic migrations. Rate limiting via slowapi. PDF generation with fpdf2. Email via SMTP.

**Frontend**: SvelteKit 5, CSS custom properties for theming, sveltekit-i18n (Italian + English), marked for markdown rendering. No CSS framework — design tokens from a shared design system.

**Extraction pipeline**: uploaded files are classified by magic bytes. Images are compressed to JPEG (max 1600px, quality 75) and sent to the OpenAI Vision API. PDFs are converted to text via markitdown. Both go into a single LLM call that returns structured JSON. The model is configurable via env var.

**Eligibility**: addresses are geocoded via Nominatim (OpenStreetMap). The reverse-geocoded municipality/postal code is checked against rules defined in the template manifest. The checker is a protocol — swap in a different implementation for polygon checks, external APIs, etc.

## Security

- **No authentication on applicant endpoints** — the UUID acts as a capability token with a 10-minute TTL. After expiry, the public API returns 410.
- **Admin endpoints** require `Authorization: Bearer <token>` with a token from `.env`.
- **Rate limiting**: bill extraction (10/hour/IP), submission creation (20/hour/IP), PDF download (5/minute/IP).
- **Path traversal protection**: file-serving endpoints validate resolved paths stay within their root directories.
- **Input validation**: fiscal code checksum and POD format enforced at the API layer. MIME types detected from magic bytes, not client headers.
- **Email sanitization**: CR/LF stripped from user-supplied data before inclusion in email headers.
- **CORS**: configurable origins, restricted to specific methods and headers.

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
# Edit .env: set OPENAI_API_KEY, DATABASE_URL, ADMIN_TOKEN

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
# In .env
TEMPLATE_DIR=./templates/my-community
```

See `templates/example/` for the manifest format.

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

The manifest declares everything the platform needs to customize for this community: name, branding, consent document versions and locations, coverage rules, wizard step order, and notification recipients. See `AGENTS.md` for the full manifest schema.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes | For bill extraction |
| `DATABASE_URL` | Yes | PostgreSQL async connection string |
| `ADMIN_TOKEN` | Yes (prod) | Bearer token for `/api/admin/*` |
| `TEMPLATE_DIR` | No | Path to community template (default: `templates/example`) |
| `EXTRACTION_MODEL` | No | OpenAI model (default: `gpt-5.4`) |
| `CORS_ORIGINS` | No | Comma-separated origins (default: localhost) |
| `DATA_DIR` | No | Where uploads and exports live (default: `./data`) |
| `SMTP_HOST` | No | SMTP server for email notifications |
| `SMTP_PORT` | No | Default: 587 |
| `SMTP_USER` | No | SMTP username |
| `SMTP_PASSWORD` | No | SMTP password |
| `SMTP_TLS` | No | Default: true |

## Development

```bash
task run:api              # backend with hot reload
task run:ui               # frontend with hot reload + API proxy
task migrate              # apply migrations
task migration -- "msg"   # create new migration
task test                 # backend + frontend tests
task lint                 # ruff + svelte-check
task export-csv           # export submissions to data/exports/
```

### Adding a field

1. Add the column to `src/celine/onboarding/models/submission.py`
2. Add to `SubmissionUpdate` and `SubmissionRead` in `models/schemas.py`
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