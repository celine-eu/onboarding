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
4. **Eligibility check** (if configured) — the applicant's address is geocoded and checked against the community's coverage area: its primary-substation boundaries, which also decide the member's area, or, for a template without boundaries, municipalities, postal codes or regions.
5. **Accept statute** — the community's founding document, presented separately from the data-collection consents. If the community enables it, this step also offers an **optional data-sharing consent**: the applicant can authorise sharing specific offers into the dataspace. It is never required and does not block submission (GDPR Art. 7(4)).
6. **Review and submit** — summary of all entered data. On submit, the applicant receives a PDF summary and the operator is notified by email.

The entire process has a 10-minute inactivity window. After that, the session token expires and the public API rejects further requests. This limits the exposure window for personal data.

### For the operator

Operators work in the console at `/admin`, signing in with their Keycloak identity; what they may do is decided by their organization and group (see [Authorization](docs/authorization.md)). They can work the queue, open a submission in full — consents, documents, extracted data, enablement — change status, repair a failed enablement step, correct a POD, name or email as a tracked revision that is then carried to every system holding a copy ([ADR-0015](docs/decisions/ADR-0015-a-correction-is-a-revision.md)), and export to CSV. The same flow is available from the terminal with `onboarding-cli admin`; see [Operator console](docs/admin-console.md). Two exports: the community's register, for its own use, and the community's dated evidence of which supply points stood authorised, and which members withdrew, under one sharing offer — kept by the community, handed to nobody, and recorded as a disclosure nowhere. `onboarding-cli export-csv` and `export-pod-list` call the same API. All admin operations are audit-logged.

Approval enables a participant in three steps, in order: a **login**, a **member in the REC registry**, then a **dataspace identity**. The login is provisioned by asking `celine-policies`' provisioning service (`PROVISIONING_URL`) — this service holds no Keycloak grant of its own; see [ADR-0004](docs/decisions/ADR-0004-ask-the-provisioning-service-instead-of-administering-the-realm.md). Approval also sends an invitation, once all three steps have succeeded and never before: Keycloak emails the participant a link to set their password, valid for 7 days, in the language they last used in the wizard. Whether it is sent is the provisioning service's decision (not for an account that already has a password, nor for a disabled one, nor twice within a short per-account cooldown), and the outcome is shown on the step in the console and by `onboarding-cli admin enablement status`; see [Operator console](docs/admin-console.md#what-approval-actually-does). A community manager can also send any registry member an invitation or a password reset from the `celine-community` dashboard. That call goes through this service's member-keyed admin routes, because this service is the provisioning service's only caller; see [API reference](docs/api-reference.md) and [ADR-0005](docs/decisions/ADR-0005-onboarding-is-the-one-caller-of-the-provisioning-service.md). Without `PROVISIONING_URL`, or for a community with no `rec_registry:` block, that step is skipped and the participant is onboarded without a login, or an invitation, and the wizard does not promise one. Registry registration fails closed — a participant missing from it is enabled in name only, invisible to every pipeline and dashboard that joins on `user_id`, POD and sensor ids. Which community they join, and their area, are per-community settings in the template manifest's `rec_registry:` block; without one, registration is skipped and the wizard still works.

When dataspace provisioning is enabled (`DATASPACE_ENABLED=true`), changing a submission to `approved` provisions a dataspace identity via the **identity-registry** HTTP API: a user DID, a Verifiable Credential, a membership in the REC organization, and a `dataspace_did` attribute on the Keycloak user. Onboarding keeps only the subject ID, DID, credential ID, and issuance timestamp. If `DS_CONNECTOR_URL` is set and the applicant gave data-sharing consent, the consented offers are then provisioned to the dataspace connector as a final, non-fatal step; a failed share leaves `share_provisioned=false` and can be retried from the console or via `POST /api/admin/{rec}/submissions/{id}/enablement/retry`. See [Dataspace Integration](docs/dataspace-integration.md) and [Data Sharing](docs/data-sharing.md) for details.

### For the community

Each REC gets a template folder that customizes the platform without code changes:

- **Branding** — name, logo, primary color (applied as CSS variables site-wide)
- **Consent documents** — local PDFs or links to external URLs, with versioning
- **Coverage area** — the community's areas as GSE primary-substation boundaries (eligibility and each member's area by boundary, synced to the REC registry by a platform admin), or municipalities, postal codes and regions for a template without boundaries
- **Wizard steps** — reorderable via the manifest (skip eligibility if no coverage restriction; a template with boundaries requires it, after `consents`)
- **Content** — markdown files for the welcome page, consent intro, and success message
- **Notifications** — sender address, operator email list, optional storage backend (S3), optional webhook

Templates are imported into the database with `task import-templates`, and served per community at `/{rec}` — one deployment hosts several.

## Architecture

**Backend**: Python 3.12, FastAPI (async), SQLAlchemy 2 (async), PostgreSQL, Alembic migrations. Rate limiting via slowapi. PDF generation with fpdf2. Email via SMTP.

**Frontend**: SvelteKit 5, CSS custom properties for theming, sveltekit-i18n (Italian, English, Spanish — wizard and operator console), marked for markdown rendering with DOMPurify sanitization. No CSS framework — design tokens from a shared design system.

**Extraction pipeline**: uploaded files are classified by magic bytes. Images are compressed to JPEG (max 1600px, quality 75) and sent to the OpenAI Vision API. PDFs are converted to text via markitdown. Both go into a single LLM call that returns structured JSON. The model is configurable via env var.

**Eligibility**: addresses are geocoded via Nominatim (OpenStreetMap). The reverse-geocoded municipality/postal code is checked against rules defined in the template manifest. A template may instead declare its areas as GSE primary-substation boundaries: the geocoded point is then sent to the Digital Twin (`DIGITAL_TWIN_URL`, `boundary_at_point`, with this service's own token) and the boundary covering it decides both eligibility and the member's area; the point is not kept, only the boundary id is recorded on the submission, and the check fails closed when the Digital Twin does not answer. See [Templates](docs/templates.md#rec-registry-binding-optional-per-community). The anonymous checks are rate-limited per client (`RATE_LIMIT_ELIGIBILITY`). When a boundary community cannot be checked, the cross-community finder answers the other communities and flags `unchecked`, and the page asks to try again later.

**Registry sync**: a template with boundary areas is the source of truth for its community's areas in the REC registry. A platform admin pushes them with `onboarding-cli registry-sync --rec <slug> --token <their token>` (or `--local`), `--dry-run` to see the plan and `--prune` to remove areas the template no longer declares (an area whose key the template renamed is moved by the registry with its members, no prune needed); the same is `POST /api/admin/recs/{rec}/registry-sync`, capability `recs.write`, which no organization group and no service account holds. A real sync first sets the community's Keycloak organization up through the provisioning service's reconcile, reported and never blocking. The registry calls use this service's own client (`OIDC_CLIENT_ID`) with `rec-registry.read`, and ask for the optional scopes `rec-registry.community.write` and `provisioning.reconcile` only for the calls that need them. The console's *Areas* page shows whether the registry still matches, to realm admins and the REC's own managers and admins (`recs.drift`). See [API reference](docs/api-reference.md) and [Operator console](docs/admin-console.md#the-registry-sync).

## Security

### Encryption at rest

All PII is encrypted using Fernet symmetric encryption (`ENCRYPTION_KEY`). This covers:

- Uploaded documents (utility bills, ID cards) encrypted on disk
- Database columns: `first_name`, `last_name`, `email`, `phone`, `fiscal_code`, `pod_code`, `consent_ip`, `supply_municipality`, `supply_boundary_id`
- JSON fields: `extracted_data`, `id_extracted_data` (OCR results), `raw_response` (LLM responses), `supply_address` (the address the eligibility step checked)

Encryption is mandatory by default. The app refuses to start without `ENCRYPTION_KEY` unless `REQUIRE_ENCRYPTION=false`, which is accepted only with `CELINE_ENV=dev`.

`ENCRYPTION_KEY` may list several keys, comma-separated: the first encrypts, all of them decrypt. To rotate, put the new key first and keep the old one after it, restart, run `onboarding-cli rotate-encryption-key` (re-encrypts every encrypted column and stored document, and encrypts any value stored without a key), then drop the old key. A value no configured key decrypts is an error, logged and raised, never returned as if it were the value; a value stored unencrypted (written with `REQUIRE_ENCRYPTION=false`) is read with a warning until the rotation encrypts it (REQ-0032).

### Session and authentication

- **Applicant sessions**: 32-byte random tokens with 10-minute inactivity TTL. All data-mutating endpoints (including extraction) require a valid session token via `X-Session-Token` header.
- **Admin endpoints** (`/api/admin/**`): a Keycloak identity, verified against the issuer's JWKS (signature, issuer, audience, expiry). Authorised by the caller's **organization + group** for operators (`admins`/`managers`/`editors`/`viewers` inside a Keycloak organization typed `rec`, valid for that organization only; the realm role `platform-admin` is the only platform-wide grant, and a realm group grants nothing) and by **scope** for service accounts, decided by OPA policies in `policies/`. Every action is audit-logged against the actor.
- **Submission emails**: the participant receives a receipt with no link; each operator receives a message of their own with a link to the submission in the admin console, where opening a document needs a sign-in and is audited. No email carries a link that opens the documents (REQ-0031).

### HTTP hardening

Security headers are enabled by default (`SECURITY_HEADERS=true`): X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy. The UI (`ui/`) sends its own Content-Security-Policy (`kit.csp` in `ui/svelte.config.js`): every page is rendered on request and SvelteKit's inline bootstrap carries a fresh nonce, so `script-src` allows no inline script; styles keep `'unsafe-inline'`. The ingress in front of the UI must not send a Content-Security-Policy of its own, since it would replace the UI's. `ui/tests/content-security-policy.spec.ts` checks it. CORS is configurable with restricted methods/headers. Rate limiting on extraction (10/hr), submission creation (20/hr), PDF download (5/min). Every limit is keyed by the client address uvicorn reports. uvicorn takes that address from `X-Forwarded-For` only when the connecting peer is listed in `FORWARDED_ALLOW_IPS` (read by uvicorn itself; default `127.0.0.1`). Behind an ingress, set `FORWARDED_ALLOW_IPS` to the ingress's address range: otherwise every visitor shares the ingress's address and one limit — for the anonymous eligibility checks, 30 an hour for the whole deployment. Never set it to `*` when the port is reachable other than through the ingress, or a caller can pick its own key.

**`FORWARDED_ALLOW_IPS` is a deployment requirement.** The same address is what the service records as a consent's evidence IP (`consent_ip`) and in every audit row. The service reads only the address uvicorn resolved and never `X-Forwarded-For` or `X-Real-IP` itself, since any caller can write those headers (REQ-0020). Without `FORWARDED_ALLOW_IPS` set to the ingress's range, every consent and audit row carries the ingress's address; with it set too wide, a caller that reaches the port directly can choose its own.

### GDPR

- Consent-first: data collection only after explicit GDPR and policy consent, with IP, timestamp, and document version recorded
- Right to erasure: `DELETE /api/admin/{rec}/submissions/{id}` removes files from disk and all DB records
- Audit trail: all admin operations logged with action, entity, IP, detail **and the operator who performed them**
- Processing agreements: bill and ID scanning send identity documents to the extraction provider, so document upload and scanning are **off** unless `EXTRACTION_ENABLED=yes`, `LLM_BASE_URL` and `LLM_VISION_MODEL` are all set — the wizard then collects personal data without documents and the document endpoints answer 403 (see [Document upload and scanning](#document-upload-and-scanning)). Phone verification follows the same pattern: a real SMS provider without `DPA_SMS_SIGNED=yes` starts with verification **off** (see [Phone Verification](#phone-verification-sms-otp))
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

The defaults wire the backend to the `celine-dev` stack with its public dev values, so a plain `docker compose up` (or `task docker:start` in `celine-dev`) on that stack runs a working onboarding: `REC_REGISTRY_URL` and `DIGITAL_TWIN_URL` on the stack's host ports through `host.docker.internal`, `PROVISIONING_URL` at the proxy's internal-only host `provisioning.internal.celine.localhost`, `OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET` as `svc-onboarding` with the dev realm's public secret, SMTP to `celine-policies`' Mailpit on `172.17.0.1:1025`, and `DATASPACE_ENABLED=false`, so no dataspace client is needed. Anything set in the shell or in `.env` overrides them; a deployment sets its own. The image installs exactly what `uv.lock` pins (`uv sync --frozen`).

### Choosing a template

```bash
task import-templates -- --filter my-community
```

See `templates/example/` for the manifest format.

## Environment Variables

### Development or deployment: `CELINE_ENV`

The defaults below make a checkout run on the celine-dev workspace with no
configuration, and each of them is refused anywhere else. **Only `CELINE_ENV=dev`
allows them.** The signal is `CELINE_ENV`, then `ENVIRONMENT`; unset, empty,
`prod`, `staging` or anything else is hardened, and startup refuses to run —
before touching the database, naming every offending setting in one message —
while any of these is in force: a development database password, the workspace's
`OIDC_BASE_URL`, the workspace's Mailpit as `SMTP_HOST` or `SMTP_TLS=false`, a
development `SMS_PROVIDER` (`log`, `console`, `dev`), `REQUIRE_ENCRYPTION=false`,
`ALLOW_PERMISSIVE_POLICY=true`, `ALLOW_LOCAL_ADMIN=true`, or a client secret equal
to its client id. In dev the same list is one warning at boot. See
[docs/specifications/deployment-posture.md](docs/specifications/deployment-posture.md).

`CELINE_ENV` is read from the process environment, not from `.env`. `task run:api`
and `task dev` export `CELINE_ENV=dev` unless the shell sets it already, so
`CELINE_ENV=staging task run:api` is the prod-like mode of the same entry point.
The compose file passes `CELINE_ENV` through with no default.

The check comes from `celine.sdk.posture`, first released in celine-sdk 2.0.0.

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
`PROVISIONING_URL`, `REC_REGISTRY_URL`, `DIGITAL_TWIN_URL`, `DS_CONNECTOR_URL`, `DS_NS_URL`,
`DS_PROVENANCE_URL`, `IDENTITY_REGISTRY_URL`, `DATASPACE_KEYCLOAK_REALM`.

### Required

| Variable | Description |
|---|---|
| `ENCRYPTION_KEY` | Fernet key for PII encryption, or several comma-separated during a rotation (first encrypts, all decrypt). Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. The one thing you must set; `REQUIRE_ENCRYPTION=false` skips it with `CELINE_ENV=dev` only |

### Defaulted, but wrong off the celine-dev workspace

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:securepassword123@172.17.0.1:15432/rec_onboarding` | The workspace's shared host Postgres, which is also where `docker compose up` puts the database |
| `OIDC_BASE_URL` | `http://keycloak.celine.localhost/realms/celine` | Keycloak realm issuer for the admin console and outbound M2M. **The one default that is silently wrong rather than merely absent**: elsewhere its JWKS is unreachable and every `/api/admin` request is denied, so startup refuses it outside `CELINE_ENV=dev` |
| `ONBOARDING_API_URL` | `http://172.17.0.1:8040` | What `onboarding-cli` talks to |

### Not defaulted, on purpose

| Variable | Description |
|---|---|
| `PROVISIONING_URL` | Internal address of `celine-policies`' provisioning service, which provisions participant logins (e.g. `http://provisioning:8010`). Empty onboards participants without a login. It must have no public route — which is also why it has no both-sides address to default to |
| `DIGITAL_TWIN_URL` | The Digital Twin (e.g. `http://digital-twin:8000`), asked which primary-substation boundary a supply address falls in and, at template import, which boundary ids exist. Needed only by a community whose template declares boundary areas; startup refuses such a template while it is empty. Called as `OIDC_CLIENT_ID` with scope `digital-twin.values.read`. `DIGITAL_TWIN_TIMEOUT` (seconds, default `5`) bounds each call; a timeout fails the check closed |

### Document upload and scanning

Off by default. Scanning sends a participant's utility bill and identity document to the model endpoint at `LLM_BASE_URL`, any OpenAI-compatible API (vLLM, Ollama, llama.cpp…). The endpoint has **no default**, so no vendor is reached by leaving it unset; to use OpenAI itself, set `https://api.openai.com/v1`. The names match celine-ai-assistant's. Unless the deployment's own operator runs that endpoint, its provider is a processor under GDPR Art. 28. `EXTRACTION_ENABLED`, `LLM_BASE_URL` and `LLM_VISION_MODEL` must all be set to switch it on. With any of them missing the app still starts, logs one warning naming what is missing, and runs without the feature:

- the wizard offers no upload on any step and drops a `utility` step, so the participant types their personal data;
- `POST /api/{rec}/extract`, `/extract-id`, `/documents/{id}/extract`, `/extractions/{id}/confirm`, and a `utility_bill` or `id_card` upload to `/submissions/{id}/documents`, answer **403** with `detail.code` `document_processing_disabled`;
- `GET /api/{rec}/config` reports `features.document_upload` and `features.document_scan`, which is what the wizard reads.

| Variable | Default | Description |
|---|---|---|
| `EXTRACTION_ENABLED` | `false` | Set to `yes` only once the endpoint at `LLM_BASE_URL` is operated by this deployment's own operator, or covered by a processing agreement that keeps processing in the EU |
| `LLM_BASE_URL` | *(none)* | OpenAI-compatible endpoint that reads the documents |
| `LLM_VISION_MODEL` | *(none)* | Vision model at that endpoint |
| `LLM_API_KEY` | *(none)* | Key for the endpoint, if it needs one (for vLLM, `--api-key`) |
| `LLM_THINKING` | `true` | `false` asks a reasoning model (Qwen on vLLM or SGLang) to skip thinking, via `chat_template_kwargs`; extraction then takes about half as long. Leave `true` for OpenAI, which refuses the field |

Some older names are no longer read:

- `DPA_SIGNED` and `OPENAI_API_KEY` were renamed on 2026-09-14.
- `EXTRACTION_API_KEY`, `EXTRACTION_BASE_URL` and `EXTRACTION_MODEL` became the generic `LLM_*` on 2026-10-01.

A deployment still setting any of them starts with scanning off and logs the rename.

### Security

| Variable | Default | Description |
|---|---|---|
| `REQUIRE_ENCRYPTION` | `true` | App refuses to start without `ENCRYPTION_KEY`. `false` is accepted with `CELINE_ENV=dev` only. |
| `OTP_HMAC_KEY` | *(none)* | Key of the phone and OTP-code hashes, separate from `ENCRYPTION_KEY`. Required outside `CELINE_ENV=dev` while phone verification is on. Changing it voids pending codes and resets per-phone rate limits |
| `SECURITY_HEADERS` | `true` | Adds security headers to all responses. Disable if your reverse proxy handles them. |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:5173` | Comma-separated allowed origins |

### Application

| Variable | Default | Description |
|---|---|---|
| `TEMPLATES_DIR` | `./templates` | Root directory templates are imported from |
| `DATA_DIR` | `./data` | Upload and export storage path |
| `MAX_UPLOAD_SIZE_MB` | `10` | Maximum file upload size |

### Email (SMTP)

The defaults are for development: they point at the Mailpit that `celine-policies`' compose publishes on the host's port 1025 (UI on 8025), the same inbox Keycloak's invitation emails land in, so nothing reaches a real person. A deployment overrides them with its relay; `SMTP_HOST=` (set, empty) switches email off. Outside `CELINE_ENV=dev` startup refuses the development host, and `SMTP_TLS=false` with a relay.

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

Optional. When a REC manifest's `steps` includes `phone_verify`, participants verify their phone via an SMS one-time code, and approval is gated on successful verification. Defaults to a `log` provider (prints the code) for local dev; outside `CELINE_ENV=dev` startup refuses `log`, `console` and `dev`, so a deployment sets `brevo` or switches verification off with `SMS_PROVIDER=none`.

A real provider receives participants' phone numbers, so it is used only with `DPA_SMS_SIGNED=yes`. Without it — or with an unknown `SMS_PROVIDER` — the app still starts and logs one warning, and phone verification is **off**: the wizard leaves the `phone_verify` step out, `verify-phone` and `confirm-phone` answer 403 with `detail.code` `phone_verification_disabled`, `GET /api/{rec}/config` reports `features.phone_verification: false`, and approval does not wait for a verification that cannot happen. The console shows that on the submission and the approval's audit row records the waiver. See [docs/phone-verification.md](docs/phone-verification.md).

| Variable | Default | Description |
|---|---|---|
| `SMS_PROVIDER` | `log` | `log` (dev only), `brevo`, or `none` to switch phone verification off |
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
| `DS_ONBOARDING_CLIENT_ID` | `svc-ds-onboarding` | The dataspace's client, for what this service does as itself (resolving organisations and subjects, the DID <-> Keycloak mapping) and, only when `DATASPACE_ENABLED` is true, the REC registry member client. With the dataspace disabled the registry member is written as `OIDC_CLIENT_ID` (`svc-onboarding`) and this client is not needed ([REQ-0022](docs/specifications/registry-member.md)) |
| `DS_ONBOARDING_CLIENT_SECRET` | *(none)* | Its secret. Required when `DATASPACE_ENABLED` is true |
| `SVC_DS_COLLECTOR_<ALIAS>_SECRET` | *(none)* | One per dataspace-bound community (alias upper-cased, `-` to `_`): the secret of `svc-ds-collector-<alias>`, the community's own client, which every act for it is made as — memberships, credentials, consent writes and read-backs, the audience read — one scope per token. Required outside `CELINE_ENV=dev`; boot refuses a missing one or one equal to the client id ([REQ-0035–REQ-0037](docs/specifications/dataspace-acting-for-a-community.md)) |
| `DS_ORG_CLIENT_ID` | *(derived)* | **Development transition only** (`CELINE_ENV=dev`, a community without a collector secret): the community's connector client the consent calls fall back to. Empty derives `svc-ds-connector-<alias>` |
| `DS_ORG_CLIENT_SECRET` | *(none)* | Its secret, for the same transition |
| `DATASPACE_USER_ROLE` | *(none)* | Role assigned in the credential |
| `DATASPACE_ALLOWED_ACTIONS` | *(none)* | Comma-separated authorized actions |
| `DATASPACE_VC_TTL_DAYS` | *(none)* | Credential validity period in days. Unset: the registry's default (30 days, renewed automatically); a value is clamped to the registry's maximum |

Which organization a community's members join, its DID, the linked participant and **which connector holds each offer's data** are **per community**, in that template's `manifest.yaml` under `dataspace:` — there is no deployment-wide equivalent, because one would file every community's members into a single organization.
| `DS_CONNECTOR_URL` | *(none)* | The community's **own** connector: its members' decisions about its own data. Empty disables share provisioning. A decision about data another participant holds is recorded at that participant's connector instead, named in the manifest's `dataspace.connectors` |
| `DS_NS_URL` | *(none)* | Public vocabulary base (`GET /ns/sharing-offers`) the wizard renders offers from; empty falls back to the connector's `/ns` path |
| `DS_PROVENANCE_URL` | *(none)* | Provenance base URL the member-facing data-sharing view reads events from; empty disables it. This service records no disclosures (ADR-0010) |

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

The manifest declares everything the platform needs to customize for this community: name, branding, consent document versions and locations, coverage rules or area boundaries, the REC registry and dataspace bindings, wizard step order, notification recipients, optional storage backend, and optional webhook. See [docs/templates.md](docs/templates.md) for the full manifest schema.

## Development

```bash
task run:api              # backend with hot reload
task run:ui               # frontend with hot reload + API proxy
task migrate              # apply migrations
task migration -- "msg"   # create new migration
task test                 # backend + frontend tests
task lint                 # ruff + svelte-check
task export-csv           # the community's register, via the API (-- --rec <slug>)
task export-pod-list      # the community's supply-point evidence for one offer, via the API
task release              # semantic-release: version, changelog, tag, push
```

### Releasing

`task release` derives the version from the conventional commits since the last tag,
writes it to `pyproject.toml` and `ui/package.json`, updates `CHANGELOG.md`, tags
`v<version>` and pushes. The release workflow (`.github/workflows/release.yaml`) builds two
images from one repository and tags both alike: `ghcr.io/celine-eu/onboarding` (the API,
`Dockerfile`) and `ghcr.io/celine-eu/onboarding-ui` (the UI, `ui/Dockerfile`). A tag
publishes `:<tag>` and `:latest`; every push to `main` publishes `:dev`. Infra deploys them
as two releases with one image-tag value.

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
