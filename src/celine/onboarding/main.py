from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from celine.onboarding.api.deps import limiter
from celine.onboarding.config.settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)

    from celine.onboarding.services.template_service import load_manifest
    manifest = load_manifest()
    steps = manifest.get("steps", [])
    if any(s in steps for s in ("utility", "identity")) and not settings.dpa_signed:
        raise RuntimeError(
            "\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            "  DPA_SIGNED=yes is required in .env\n"
            "═══════════════════════════════════════════════════════════════\n\n"
            "This instance uses LLM-based extraction (bill/ID processing),\n"
            "which sends personal data to an external AI provider.\n\n"
            "GDPR Article 28 requires a Data Processing Agreement (DPA)\n"
            "with your provider before processing personal data.\n\n"
            "  1. Sign the DPA with your LLM provider:\n"
            "     - OpenAI:  https://privacy.openai.com\n"
            "     - Mistral: https://mistral.ai/terms/#dpa\n"
            "     - Azure:   covered by your Microsoft DPA\n"
            "  2. Download and keep a copy on file for audits\n"
            "  3. Set DPA_SIGNED=yes in your .env file\n\n"
            "═══════════════════════════════════════════════════════════════\n"
        )

    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="CER Onboarding",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.limiter = limiter

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Please try again later."},
        )

    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )

    from celine.onboarding.api.health import router as health_router
    from celine.onboarding.api.config import router as config_router
    from celine.onboarding.api.submissions import router as submissions_router
    from celine.onboarding.api.documents import router as documents_router
    from celine.onboarding.api.extractions import router as extractions_router
    from celine.onboarding.api.consent_documents import router as consent_docs_router
    from celine.onboarding.api.eligibility import router as eligibility_router
    from celine.onboarding.api.admin import router as admin_router

    app.include_router(health_router, prefix="/api")
    app.include_router(config_router, prefix="/api")
    app.include_router(submissions_router, prefix="/api")
    app.include_router(documents_router, prefix="/api")
    app.include_router(extractions_router, prefix="/api")
    app.include_router(consent_docs_router, prefix="/api")
    app.include_router(eligibility_router, prefix="/api")
    app.include_router(admin_router, prefix="/api/admin")

    return app


app = create_app()
