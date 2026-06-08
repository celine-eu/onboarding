FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml .
RUN uv sync --no-dev --no-install-project

COPY src/ .
COPY alembic.ini .
COPY alembic/ alembic/
RUN uv sync --no-dev

EXPOSE 8040

CMD ["uv", "run", "uvicorn", "celine.onboarding.main:app", "--host", "0.0.0.0", "--port", "8040"]
