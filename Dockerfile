FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml .
RUN uv sync --no-dev --no-install-project

COPY src/ src/
COPY templates/ templates/
# OPA policies for the admin console, evaluated in-process. Without them the
# access policy cannot load and every /api/admin request is denied — so this is
# a runtime dependency, not a development artefact.
COPY policies/ policies/
COPY alembic.ini .
COPY alembic/ alembic/
RUN uv sync --no-dev

EXPOSE 8040

CMD ["uv", "run", "uvicorn", "celine.onboarding.main:app", "--host", "0.0.0.0", "--port", "8040"]
