from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from celine.onboarding.api.deps import limiter
from celine.onboarding.config.settings import settings


async def _validate_dataspace_config() -> None:
    """Refuse to start on a dataspace misconfiguration a REC manager would hit.

    Dataspace integration is optional per community — a REC with no ``dataspace``
    block runs the full wizard, collects no sharing consent and provisions no
    identity. That is supported, not degraded. But once a community *is* bound,
    a missing organisation or a missing offers vocabulary must surface here
    rather than mid-review: onboarding never creates dataspace state, so there is
    nothing for it to fall back to.
    """
    from celine.onboarding.services.template_service import (
        dataspace_binding,
        get_slugs,
        load_manifest,
        rec_registry_binding,
    )

    for slug in get_slugs():
        manifest = load_manifest(slug)
        binding = dataspace_binding(slug)  # raises on a malformed block
        registry = rec_registry_binding(slug)  # raises on a malformed block

        if registry.enabled:
            # The wrapper methods this integration calls are unreleased, so an
            # environment that installed celine-sdk from the index has the
            # generated client but not the wrapper. Catch it at boot rather than
            # as an AttributeError the first time a REC manager approves
            # somebody. Delete this once the version constraint can express it.
            from celine.sdk.rec_registry.client import RecRegistryAdminClient

            if not hasattr(RecRegistryAdminClient, "create_member"):
                raise RuntimeError(
                    f"\n\n"
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"  celine-sdk is too old for REC registry registration\n"
                    f"═══════════════════════════════════════════════════════════════\n\n"
                    f"REC '{slug}' declares a rec_registry block, but the installed\n"
                    f"celine-sdk has no RecRegistryAdminClient.create_member. The\n"
                    f"write wrappers are not released yet.\n\n"
                    f"  1. task sdk:local     (uv pip install -e ../celine-sdk)\n"
                    f"  2. Or remove the rec_registry block from the manifest\n\n"
                    f"═══════════════════════════════════════════════════════════════\n"
                )

        if registry.enabled and not settings.rec_registry_url:
            raise RuntimeError(
                f"\n\n"
                f"═══════════════════════════════════════════════════════════════\n"
                f"  REC_REGISTRY_URL is required (REC: {slug})\n"
                f"═══════════════════════════════════════════════════════════════\n\n"
                f"REC '{slug}' declares a rec_registry block, so approving a\n"
                f"participant has to register them as a community member. With no\n"
                f"URL configured that step cannot run, and approval would enable\n"
                f"somebody who is invisible to every pipeline downstream.\n\n"
                f"  1. Set REC_REGISTRY_URL in your .env file\n"
                f"  2. Or remove the rec_registry block from the manifest\n\n"
                f"═══════════════════════════════════════════════════════════════\n"
            )

        # Asking for a sharing consent means rendering the offers from the
        # published vocabulary. With none configured the step vanishes silently,
        # which is indistinguishable from "this community shares nothing" — so
        # the person is never asked and nobody finds out.
        declares_sharing = (manifest.get("consent") or {}).get("data_sharing") is not None
        if declares_sharing and not (settings.ds_ns_url or settings.ds_connector_url):
            raise RuntimeError(
                f"\n\n"
                f"═══════════════════════════════════════════════════════════════\n"
                f"  DS_NS_URL or DS_CONNECTOR_URL is required (REC: {slug})\n"
                f"═══════════════════════════════════════════════════════════════\n\n"
                f"REC '{slug}' declares consent.data_sharing, so the wizard has to\n"
                f"render sharing offers from the published vocabulary\n"
                f"(GET /ns/sharing-offers). With neither URL set there is nothing\n"
                f"to render and the step would disappear without a trace.\n\n"
                f"  1. Set DS_NS_URL (or DS_CONNECTOR_URL) in your .env file\n"
                f"  2. Or remove consent.data_sharing from the manifest\n\n"
                f"═══════════════════════════════════════════════════════════════\n"
            )

        if not (binding.enabled and settings.dataspace_enabled):
            continue

        from celine.onboarding.services.dataspace_identity import organization_exists

        if await organization_exists(binding.organization) is False:
            raise RuntimeError(
                f"\n\n"
                f"═══════════════════════════════════════════════════════════════\n"
                f"  Dataspace organization '{binding.organization}' does not exist\n"
                f"═══════════════════════════════════════════════════════════════\n\n"
                f"REC '{slug}' is bound to it, but the identity registry has no\n"
                f"such owner. Onboarding deliberately does not create one: an\n"
                f"organization minted from an approval carries no verification and\n"
                f"no agreement, so it declares no capacity — and capacity is what\n"
                f"decides whether a recipient is disclosed or must be consented to.\n\n"
                f"  1. Seed the organization from the deployment's owners.yaml\n"
                f"  2. Have an operator take it through the registry's\n"
                f"     verify -> agreement -> credential -> promote chain\n"
                f"  3. Or remove the 'dataspace' block from the REC's manifest\n\n"
                f"═══════════════════════════════════════════════════════════════\n"
            )


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

    # A real SMS gateway receives the participant's phone number, making it a
    # processor under GDPR Art. 28 exactly as the LLM provider is above.
    sms_is_real = settings.sms_provider.strip().lower() not in {"log", "console", "dev"}
    if sms_is_real and not settings.dpa_sms_signed:
        raise RuntimeError(
            f"\n\n"
            f"═══════════════════════════════════════════════════════════════\n"
            f"  DPA_SMS_SIGNED=yes is required in .env\n"
            f"═══════════════════════════════════════════════════════════════\n\n"
            f"SMS_PROVIDER={settings.sms_provider} sends participant phone\n"
            f"numbers to an external SMS gateway.\n\n"
            f"GDPR Article 28 requires a Data Processing Agreement (DPA)\n"
            f"with your provider before processing personal data.\n\n"
            f"  1. Sign the DPA with your SMS provider\n"
            f"  2. Set DPA_SMS_SIGNED=yes in your .env file\n\n"
            f"For development, use SMS_PROVIDER=log instead.\n\n"
            f"═══════════════════════════════════════════════════════════════\n"
        )

    await _validate_dataspace_config()

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
        title="REC Onboarding",
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
    from celine.onboarding.api.phone_verify import router as phone_verify_router
    from celine.onboarding.api.admin import router as admin_router

    app.include_router(health_router, prefix="/api")
    app.include_router(recs_router, prefix="/api")
    app.include_router(downloads_router, prefix="/api")

    app.include_router(config_router, prefix="/api/{rec_slug}")
    app.include_router(submissions_router, prefix="/api/{rec_slug}")
    app.include_router(phone_verify_router, prefix="/api/{rec_slug}")
    app.include_router(documents_router, prefix="/api/{rec_slug}")
    app.include_router(extractions_router, prefix="/api/{rec_slug}")
    app.include_router(consent_docs_router, prefix="/api/{rec_slug}")
    app.include_router(eligibility_router, prefix="/api/{rec_slug}")
    app.include_router(admin_router, prefix="/api/{rec_slug}/admin")

    return app


app = create_app()
