FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# `uv run` in the image (and the compose commands) uses the lock as it is and
# never re-resolves or rewrites it.
ENV UV_FROZEN=1

# The lock, not a fresh resolution: `--frozen` installs exactly what uv.lock
# pins, so an image carries the dependency versions the tests ran against.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
# pyproject.toml declares it as the package readme; hatchling refuses to build
# the project without it.
COPY README.md .
COPY templates/ templates/
# OPA policies for the admin console, evaluated in-process. Without them the
# access policy cannot load and every /api/admin request is denied — so this is
# a runtime dependency, not a development artefact.
COPY policies/ policies/
COPY alembic.ini .
COPY alembic/ alembic/
RUN uv sync --frozen --no-dev

EXPOSE 8040

CMD ["uv", "run", "uvicorn", "celine.onboarding.main:app", "--host", "0.0.0.0", "--port", "8040"]
