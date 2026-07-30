import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from celine.onboarding.api.deps import limiter
from celine.onboarding.config.settings import settings
from celine.onboarding.security.middleware import AdminAuthMiddleware

logger = logging.getLogger(__name__)


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
        organization_for,
        rec_registry_binding,
        validate_organization,
    )

    for slug in get_slugs():
        manifest = load_manifest(slug)
        # Manifests are read from the database, so they may have been imported by
        # an older build than the one now booting. Re-validate rather than trust
        # that `import-templates` was the gate.
        validate_organization(manifest, where=f"REC {slug!r}")
        binding = dataspace_binding(slug)  # raises on a malformed block
        registry = rec_registry_binding(slug)  # raises on a malformed block

        if not organization_for(slug):
            # Not fatal: a single-community deployment can be run entirely by
            # platform operators holding realm-level groups. But per-community
            # delegation is impossible without an organisation, and finding that
            # out by being denied is worse than being told at boot.
            logger.warning(
                "REC %r declares no 'organization', so no per-community operator "
                "can be granted access to it — only platform operators holding a "
                "realm-level group. Add 'organization: <keycloak-org-alias>' to "
                "its manifest to delegate its review queue.",
                slug,
            )

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


def _validate_admin_config() -> None:
    """Refuse to start with an admin console that is not actually protected.

    Each of these is a configuration in which the console *appears* guarded and is
    not, which is worse than one that is obviously broken.
    """
    from celine.onboarding.api.admin.deps import RESERVED_SLUGS
    from celine.onboarding.security.oidc import is_configured, oidc_settings
    from celine.onboarding.security.policy import get_policy
    from celine.onboarding.services.template_service import get_slugs

    if settings.removed_admin_token:
        raise RuntimeError(
            "\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            "  ADMIN_TOKEN is set, and no longer does anything\n"
            "═══════════════════════════════════════════════════════════════\n\n"
            "The shared admin token was replaced by Keycloak identities and OPA\n"
            "policies: operators are authorised by their organization and group,\n"
            "and every action is recorded against them by name.\n\n"
            "Leaving the variable set would read as protection that is not there,\n"
            "so startup refuses it rather than ignoring it.\n\n"
            "  1. Remove ADMIN_TOKEN from your .env and environment\n"
            "  2. Configure OIDC_BASE_URL and give operators a group in their\n"
            "     community's Keycloak organization\n"
            "  3. For a deployment with no Keycloak, use `onboarding-cli --local`\n\n"
            "═══════════════════════════════════════════════════════════════\n"
        )

    if not is_configured():
        raise RuntimeError(
            "\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            "  OIDC_BASE_URL is required\n"
            "═══════════════════════════════════════════════════════════════\n\n"
            "Admin console tokens are verified against the issuer's JWKS. With no\n"
            "issuer configured there is no key to check a signature against, and\n"
            "every /api/admin request would fail closed at runtime.\n\n"
            "  1. Set OIDC_BASE_URL, e.g.\n"
            "     http://keycloak.celine.localhost/realms/celine\n"
            "  2. Optionally override OIDC_JWKS_URI if it is not the realm's\n"
            "     /protocol/openid-connect/certs\n\n"
            f"(resolved issuer={oidc_settings().base_url!r} "
            f"jwks={oidc_settings().jwks_uri!r})\n\n"
            "═══════════════════════════════════════════════════════════════\n"
        )

    policy = get_policy()
    if not policy.available and not settings.allow_permissive_policy:
        raise RuntimeError(
            "\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            "  Access policies could not be loaded\n"
            "═══════════════════════════════════════════════════════════════\n\n"
            f"{policy.load_error}\n\n"
            "Every /api/admin request would be denied, so the console would be\n"
            "unusable rather than insecure — but the cause is worth fixing at boot\n"
            "instead of discovering it one denial at a time.\n\n"
            "  1. Check POLICIES_DIR points at the repo's policies/ directory\n"
            "  2. For development without policies, set\n"
            "     ALLOW_PERMISSIVE_POLICY=true — which allows EVERYTHING\n\n"
            "═══════════════════════════════════════════════════════════════\n"
        )

    # A REC slug that collides with a literal segment of the admin router would be
    # unreachable: /api/admin/recs is the community list, not the REC named "recs".
    colliding = sorted(set(get_slugs()) & RESERVED_SLUGS)
    if colliding:
        raise RuntimeError(
            "\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            f"  REC slug(s) reserved by the admin API: {', '.join(colliding)}\n"
            "═══════════════════════════════════════════════════════════════\n\n"
            f"The admin router uses {', '.join(sorted(RESERVED_SLUGS))} as literal\n"
            "path segments under /api/admin, so a community with one of those\n"
            "slugs could never be addressed there.\n\n"
            "  1. Rename the template directory and its manifest slug\n\n"
            "═══════════════════════════════════════════════════════════════\n"
        )

    if settings.allow_permissive_policy:
        logger.warning(
            "ALLOW_PERMISSIVE_POLICY is on — every admin request is allowed when "
            "the policy engine is unavailable. Never set this in production."
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
    _validate_admin_config()

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

    # Rejects an unauthenticated /api/admin request before routing, so the
    # console's route shapes are not discoverable without credentials. Only that
    # prefix — the wizard is anonymous by design.
    app.add_middleware(AdminAuthMiddleware)

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
    from celine.onboarding.api.admin import create_admin_router

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
    # One prefix for the whole authenticated surface; the REC is a segment
    # inside it. Mounted last so its literal paths cannot be shadowed.
    app.include_router(create_admin_router())

    return app


app = create_app()
