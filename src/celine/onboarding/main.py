from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from celine.onboarding.api.deps import limiter
from celine.onboarding.config.settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)

    from celine.onboarding.services.template_service import load_recs_from_db
    await load_recs_from_db()

    from celine.onboarding.services.template_service import get_slugs, load_manifest

    for slug in get_slugs():
        manifest = load_manifest(slug)
        steps = manifest.get("steps", [])
        if any(s in steps for s in ("utility", "identity", "personal")) and not settings.dpa_signed:
            raise RuntimeError(
                f"\n\n"
                f"═══════════════════════════════════════════════════════════════\n"
                f"  DPA_SIGNED=yes is required in .env (REC: {slug})\n"
                f"═══════════════════════════════════════════════════════════════\n\n"
                f"REC '{slug}' uses LLM-based extraction (bill/ID processing),\n"
                f"which sends personal data to an external AI provider.\n\n"
                f"GDPR Article 28 requires a Data Processing Agreement (DPA)\n"
                f"with your provider before processing personal data.\n\n"
                f"  1. Sign the DPA with your LLM provider\n"
                f"  2. Set DPA_SIGNED=yes in your .env file\n\n"
                f"═══════════════════════════════════════════════════════════════\n"
            )

    if settings.require_encryption and not settings.encryption_key:
        raise RuntimeError(
            "\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            "  ENCRYPTION_KEY is required\n"
            "═══════════════════════════════════════════════════════════════\n\n"
            "PII encryption is mandatory for production deployments.\n\n"
            "Generate a key:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"\n\n'
            "Then set ENCRYPTION_KEY in your .env file.\n\n"
            "For development only, set REQUIRE_ENCRYPTION=false to skip.\n\n"
            "═══════════════════════════════════════════════════════════════\n"
        )

    yield


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response


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

    if settings.security_headers:
        app.add_middleware(SecurityHeadersMiddleware)

    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Session-Token"],
    )

    from celine.onboarding.api.health import router as health_router
    from celine.onboarding.api.recs import router as recs_router
    from celine.onboarding.api.downloads import router as downloads_router
    from celine.onboarding.api.config import router as config_router
    from celine.onboarding.api.submissions import router as submissions_router
    from celine.onboarding.api.documents import router as documents_router
    from celine.onboarding.api.extractions import router as extractions_router
    from celine.onboarding.api.consent_documents import router as consent_docs_router
    from celine.onboarding.api.eligibility import router as eligibility_router
    from celine.onboarding.api.admin import router as admin_router

    app.include_router(health_router, prefix="/api")
    app.include_router(recs_router, prefix="/api")
    app.include_router(downloads_router, prefix="/api")

    app.include_router(config_router, prefix="/api/{rec_slug}")
    app.include_router(submissions_router, prefix="/api/{rec_slug}")
    app.include_router(documents_router, prefix="/api/{rec_slug}")
    app.include_router(extractions_router, prefix="/api/{rec_slug}")
    app.include_router(consent_docs_router, prefix="/api/{rec_slug}")
    app.include_router(eligibility_router, prefix="/api/{rec_slug}")
    app.include_router(admin_router, prefix="/api/{rec_slug}/admin")

    return app


app = create_app()
