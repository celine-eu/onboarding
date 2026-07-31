#!/usr/bin/env bash
# End-to-end: a real database, a real app, a real browser.
#
# Boots a throwaway Postgres (unless one is given), a test issuer whose JWKS the
# app really fetches, the API and the built UI; then runs the Python e2e suite
# and the Playwright suite against them.
#
#   ./scripts/e2e.sh            everything
#   ./scripts/e2e.sh api        Python only
#   ./scripts/e2e.sh ui         Playwright only
set -euo pipefail

cd "$(dirname "$0")/.."
WHAT="${1:-all}"

: "${E2E_PG_PORT:=15499}"
: "${E2E_DB:=onboarding_e2e}"
: "${E2E_IDP_PORT:=18099}"
: "${E2E_API_PORT:=18040}"
: "${E2E_UI_PORT:=13000}"
: "${E2E_REC:=e2e-rec}"

export ONBOARDING_E2E_DATABASE_URL="${ONBOARDING_E2E_DATABASE_URL:-postgresql+asyncpg://postgres:securepassword123@localhost:${E2E_PG_PORT}/${E2E_DB}}"

# Refuse to run against ports somebody else is holding: otherwise a leftover
# server from an aborted run silently becomes the system under test.
for port in "$E2E_IDP_PORT" "$E2E_API_PORT" "$E2E_UI_PORT"; do
  if (echo >"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    echo "port $port is already in use — a previous run may still be up" >&2
    exit 1
  fi
done

PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  [ "${STARTED_PG:-0}" = 1 ] && docker rm -f onb-e2e-pg >/dev/null 2>&1 || true
}
trap cleanup EXIT

# --- database -----------------------------------------------------------------
if ! pg_isready -h localhost -p "$E2E_PG_PORT" -q 2>/dev/null; then
  echo "==> starting throwaway Postgres on :$E2E_PG_PORT"
  docker run -d --rm --name onb-e2e-pg -e POSTGRES_PASSWORD=securepassword123 \
    -e POSTGRES_DB=postgres -p "$E2E_PG_PORT:5432" postgres:16 >/dev/null
  STARTED_PG=1
  for _ in $(seq 1 40); do pg_isready -h localhost -p "$E2E_PG_PORT" -q && break; sleep 1; done
fi
PGPASSWORD=securepassword123 psql -h localhost -p "$E2E_PG_PORT" -U postgres -q \
  -c "SELECT 1 FROM pg_database WHERE datname='${E2E_DB}'" | grep -q 1 || \
  PGPASSWORD=securepassword123 psql -h localhost -p "$E2E_PG_PORT" -U postgres -q \
    -c "CREATE DATABASE ${E2E_DB}"

# --- python suite -------------------------------------------------------------
# It boots and tears down its own API, so it needs nothing else running.
if [ "$WHAT" = all ] || [ "$WHAT" = api ]; then
  echo "==> API and CLI end-to-end"
  DATABASE_URL="$ONBOARDING_E2E_DATABASE_URL" uv run --project src pytest tests/e2e -q
fi

# --- browser suite ------------------------------------------------------------
if [ "$WHAT" = all ] || [ "$WHAT" = ui ]; then
  echo "==> issuer on :$E2E_IDP_PORT"
  uv run --project src python tests/e2e/idp.py "$E2E_IDP_PORT" community-a admins > /tmp/onb-e2e-tokens &
  PIDS+=($!)
  for _ in $(seq 1 30); do [ -s /tmp/onb-e2e-tokens ] && break; sleep 0.5; done
  OPERATOR_TOKEN="$(python3 -c "import json;print(json.load(open('/tmp/onb-e2e-tokens'))['operator'])")"
  DENIED_TOKEN="$(python3 -c "import json;print(json.load(open('/tmp/onb-e2e-tokens'))['denied'])")"

  echo "==> migrating and seeding"
  DATABASE_URL="$ONBOARDING_E2E_DATABASE_URL" uv run --project src alembic upgrade head >/dev/null
  DATABASE_URL="$ONBOARDING_E2E_DATABASE_URL" uv run --project src python - <<'PY'
import asyncio, os, uuid
from sqlalchemy import select
from celine.onboarding.models.database import async_session
from celine.onboarding.models.rec import Rec
from celine.onboarding.models.submission import Submission, SubmissionStatus

REC = os.environ.get("E2E_REC", "e2e-rec")

async def main():
    async with async_session() as db:
        rec = (await db.execute(select(Rec).where(Rec.slug == REC))).scalar_one_or_none()
        manifest = {"slug": REC, "name": "E2E Community", "organization": "community-a",
                    "steps": ["consents", "personal", "review"]}
        if rec:
            rec.manifest = manifest
        else:
            db.add(Rec(slug=REC, name="E2E Community", manifest=manifest, active=True))
        await db.commit()

        count = len((await db.execute(select(Submission).where(Submission.rec_slug == REC))).scalars().all())
        for _ in range(max(0, 3 - count)):
            db.add(Submission(rec_slug=REC, status=SubmissionStatus.UNDER_REVIEW,
                              consent_ip="127.0.0.1", first_name="Mario", last_name="Rossi",
                              email=f"m-{uuid.uuid4().hex[:6]}@example.org",
                              fiscal_code="RSSMRA85T10A562S", pod_code="IT001E12345678",
                              gdpr_consent=True, policy_consent=True, statute_consent=True))
        await db.commit()
asyncio.run(main())
PY

  echo "==> API on :$E2E_API_PORT"
  DATABASE_URL="$ONBOARDING_E2E_DATABASE_URL" \
  OIDC_BASE_URL="http://127.0.0.1:$E2E_IDP_PORT" \
  OIDC_JWKS_URI="http://127.0.0.1:$E2E_IDP_PORT/certs" \
  REQUIRE_ENCRYPTION=false DPA_SIGNED=yes DPA_SMS_SIGNED=yes ADMIN_TOKEN= \
  DS_NS_URL= DS_CONNECTOR_URL= REC_REGISTRY_URL= DATASPACE_ENABLED=false \
    uv run --project src uvicorn celine.onboarding.main:app --port "$E2E_API_PORT" --log-level warning &
  PIDS+=($!)
  for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:$E2E_API_PORT/api/health" >/dev/null && break; sleep 0.5; done

  echo "==> UI on :$E2E_UI_PORT"
  (cd ui && pnpm build >/dev/null)
  ( cd ui && PORT="$E2E_UI_PORT" API_BASE_URL="http://127.0.0.1:$E2E_API_PORT" \
      exec node build/index.js ) &
  PIDS+=($!)
  for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:$E2E_UI_PORT/" >/dev/null && break; sleep 0.5; done

  echo "==> Playwright"
  (cd ui && OPERATOR_TOKEN="$OPERATOR_TOKEN" DENIED_TOKEN="$DENIED_TOKEN" E2E_REC="$E2E_REC" \
     PLAYWRIGHT_BASE_URL="http://127.0.0.1:$E2E_UI_PORT" pnpm exec playwright test "$@" --reporter=list)
fi

echo "==> done"
