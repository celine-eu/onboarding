import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from celine.onboarding.api.deps import limiter
from celine.onboarding.config.settings import Settings, settings
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

        from celine.onboarding.services.dataspace_identity import check_organization

        owner = await check_organization(binding.organization)

        if owner.found is False:
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

        # Existing is not the same as admissible. The registry gained a lifecycle
        # in August — verified, suspended, revoked — and the checks above only
        # ever asked whether a row was there. Filing new members into a suspended
        # organisation is the admission decision the registry's own enrolment
        # service refuses to let a tool take: it will not issue an enrolment token
        # for an owner that is not verified, on the grounds that doing so is a
        # governance decision taken by whoever ran the command. Provisioning a
        # person into one is the same act.
        #
        # A registry that reports no status at all leaves `status` None and this
        # check passes — an absent field must not read as "not verified".
        if owner.found and owner.status and owner.status != "verified":
            raise RuntimeError(
                f"\n\n"
                f"═══════════════════════════════════════════════════════════════\n"
                f"  Dataspace organization '{binding.organization}' is "
                f"{owner.status}\n"
                f"═══════════════════════════════════════════════════════════════\n\n"
                f"REC '{slug}' is bound to it, and only a *verified* organization\n"
                f"may take on new members. A suspended or revoked owner has had\n"
                f"its register bits cleared and its participants deactivated, so\n"
                f"anyone onboarded into it would hold a credential that authorises\n"
                f"nothing — and would have been told otherwise.\n\n"
                f"  1. Have an operator reinstate it in the identity registry\n"
                f"  2. Or remove the 'dataspace' block from the REC's manifest\n"
                f"     until it is reinstated\n\n"
                f"═══════════════════════════════════════════════════════════════\n"
            )


def _warn_document_processing() -> None:
    """Say once, at boot, that document upload and scanning are off, and why.

    This used to refuse to start. A missing processing agreement is a reason to
    switch off the one feature that sends identity documents to a third party,
    not to take the whole onboarding down: the wizard works from the fields the
    participant types, and the API refuses the document routes on its own.
    """
    if settings.document_processing_enabled:
        return
    missing = [
        name
        for name, present in (
            ("DPA_SIGNED", settings.dpa_signed),
            ("OPENAI_API_KEY", bool(settings.openai_api_key)),
        )
        if not present
    ]
    logger.warning(
        "Document upload and scanning are disabled: %s not set. The wizard collects "
        "personal data without a bill or ID card, and the upload and extraction "
        "endpoints answer 403. Scanning sends identity documents to the extraction "
        "provider (%s), so enable it only under a data processing agreement with "
        "that provider (GDPR Art. 28).",
        " and ".join(missing),
        settings.extraction_base_url,
    )


def _validate_provisioning_config() -> None:
    """Refuse to start when approving somebody could not give them a login.

    Provisioning is step 1 of enablement and fails closed, so a missing setting
    here does not degrade anything — it stops a review, in front of an operator
    who can do nothing about it. Every other outbound dependency is checked at
    boot for that reason and this one was not.

    The settings this used to check are gone with the Admin API calls they
    configured. What is left is one address, one credential, and the two
    families of leftover values that must not be allowed to look like
    configuration.
    """
    credentials = [
        name
        for name, value in (
            ("DATASPACE_KEYCLOAK_ADMIN_USERNAME", settings.removed_keycloak_admin_username),
            ("DATASPACE_KEYCLOAK_ADMIN_PASSWORD", settings.removed_keycloak_admin_password),
            (
                "DATASPACE_KEYCLOAK_ADMIN_CLIENT_SECRET",
                settings.removed_keycloak_admin_client_secret,
            ),
        )
        if value
    ]
    if credentials:
        raise RuntimeError(
            "\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            f"  {', '.join(credentials)} is set, and no longer does anything\n"
            "═══════════════════════════════════════════════════════════════\n\n"
            "Participant logins used to be provisioned by logging in as a\n"
            "Keycloak administrator: a person's username and password, against\n"
            "the master realm, in the environment of the service that serves the\n"
            "public wizard. They are now provisioned by celine-policies'\n"
            "provisioning service, and this service administers no realm at all.\n\n"
            "Nothing reads these any more, and an administrator's password that\n"
            "nothing reads is still an administrator's password in a deployment's\n"
            "environment — so startup refuses it rather than leaving it there.\n\n"
            "  1. Remove them from your .env and environment\n"
            "  2. Rotate the credential: it has been readable by this process\n"
            "  3. Set PROVISIONING_URL to reach the provisioning service\n\n"
            "═══════════════════════════════════════════════════════════════\n"
        )

    # The group-scoped grant's own settings. Inert rather than dangerous — but
    # DATASPACE_KEYCLOAK_ENABLED=true reads as "participants are being given
    # logins", and with nothing reading it none would be. That is the failure
    # this refusal exists for, and the other three travel in the same .env.
    grant = [
        name
        for name, value in (
            ("DATASPACE_KEYCLOAK_ENABLED", settings.removed_keycloak_enabled),
            ("DATASPACE_KEYCLOAK_BASE_URL", settings.removed_keycloak_base_url),
            (
                "DATASPACE_KEYCLOAK_PARTICIPANTS_GROUP",
                settings.removed_keycloak_participants_group,
            ),
            ("DATASPACE_KEYCLOAK_UPDATE_EXISTING", settings.removed_keycloak_update_existing),
        )
        if value
    ]
    if grant:
        raise RuntimeError(
            "\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            f"  {', '.join(grant)} is set, and no longer does anything\n"
            "═══════════════════════════════════════════════════════════════\n\n"
            "This service held a Keycloak grant over one realm group and created\n"
            "participants into it. It holds no grant now: celine-policies runs a\n"
            "provisioning service that is the only writer of participant\n"
            "accounts, and this service calls it — which is also what finally\n"
            "puts a participant in their community's Keycloak ORGANIZATION, the\n"
            "claim every org-scoped policy resolves them by. No fine-grained\n"
            "group permission can do that, which is why the grant went rather\n"
            "than being narrowed again.\n\n"
            "DATASPACE_KEYCLOAK_ENABLED=true is the dangerous one to leave: it\n"
            "reads as 'participants are being given logins' and nothing reads it,\n"
            "so none would be — silently, one approval at a time.\n\n"
            "  1. Remove them from your .env and environment\n"
            "  2. Set PROVISIONING_URL to the provisioning service's INTERNAL\n"
            "     address (http://provisioning:8010 under compose). It must not\n"
            "     have a public route: it holds realm-wide administration and is\n"
            "     safe to hold it only because nothing outside can reach it\n"
            "  3. Or leave PROVISIONING_URL unset, which onboards participants\n"
            "     without giving them a login — what ENABLED=false used to mean\n\n"
            "  DATASPACE_KEYCLOAK_REALM is unaffected and stays. It names the\n"
            "  realm the account lives in for the dataspace step, and administers\n"
            "  nothing.\n\n"
            "═══════════════════════════════════════════════════════════════\n"
        )

    if not settings.provisioning_url.strip():
        return

    from celine.onboarding.services.service_auth import issuer_realm

    if not settings.oidc_client_secret.strip():
        raise RuntimeError(
            "\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            "  OIDC_CLIENT_SECRET is required\n"
            "═══════════════════════════════════════════════════════════════\n\n"
            "Giving somebody a login in the celine realm is celine's own\n"
            "business, so the provisioning service is called as celine's own\n"
            "client rather than the dataspace's. Without the secret there is no\n"
            "token to present to it.\n\n"
            f"  1. Set OIDC_CLIENT_SECRET for client\n"
            f"     '{settings.oidc_client_id}' in your .env file\n"
            "  2. Grant that client the scope 'provisioning.participants.write'\n"
            "     and an audience mapper onto svc-provisioning: celine-policies\n"
            "     declares both in clients.yaml, then run `keycloak sync`\n\n"
            "═══════════════════════════════════════════════════════════════\n"
        )

    # The realm is not administered from here, but it is still *reported*: the
    # dataspace step tells the identity registry which realm the account lives
    # in, and an unset DATASPACE_KEYCLOAK_REALM means "the realm the issuer
    # names". A deployment whose issuer names none has to say.
    minting_realm = issuer_realm(settings.oidc_base_url)
    target_realm = settings.dataspace_keycloak_realm.strip()

    if not target_realm and not minting_realm:
        raise RuntimeError(
            "\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            "  DATASPACE_KEYCLOAK_REALM is required\n"
            "═══════════════════════════════════════════════════════════════\n\n"
            "It is normally left unset, because the realm a participant's\n"
            "account lives in is the realm OIDC_BASE_URL issues from. That URL\n"
            f"names none: {settings.oidc_base_url!r} is not a Keycloak realm\n"
            "issuer, so the realm has to be stated — the dataspace step hands it\n"
            "to the identity registry, which is how anything finds the account\n"
            "again.\n\n"
            "  1. Set DATASPACE_KEYCLOAK_REALM in your .env file\n"
            "  2. Or point OIDC_BASE_URL at the realm, e.g.\n"
            "     http://keycloak.example/realms/<realm>\n\n"
            "═══════════════════════════════════════════════════════════════\n"
        )

    if target_realm and minting_realm and minting_realm != target_realm:
        raise RuntimeError(
            "\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            f"  Keycloak realms disagree: '{minting_realm}' vs '{target_realm}'\n"
            "═══════════════════════════════════════════════════════════════\n\n"
            "The argument has changed and the refusal has not. It used to be\n"
            "that this service presented its own token to the Admin API, and a\n"
            "client-credentials token administers the realm that minted it and\n"
            "no other. It administers nothing now — but the two values still\n"
            "have to agree, for a different reason:\n\n"
            f"  the provisioning service writes the account into '{minting_realm}',\n"
            f"  the realm this deployment's own issuer names, and the dataspace\n"
            f"  step would then tell the identity registry it is in\n"
            f"  '{target_realm}'. Every lookup that follows looks in the wrong\n"
            "  realm and finds nothing — with no error, because an absent user\n"
            "  and a user in another realm are the same answer.\n\n"
            f"  1. Set DATASPACE_KEYCLOAK_REALM={minting_realm}, or unset it — it\n"
            "     defaults to the realm the issuer names\n"
            "  2. Or point OIDC_BASE_URL at the realm the participants are in\n\n"
            "═══════════════════════════════════════════════════════════════\n"
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

    # The issuer has a development default, so the refusal above no longer fires
    # for a deployment that merely forgot the variable — only for one that set it
    # empty on purpose. That trade is why this warning exists: an unreachable
    # JWKS still fails closed on every /api/admin request, but a production
    # deployment would otherwise learn it one denied operator at a time instead
    # of at boot. It is the one default here that is silently wrong off this
    # workspace rather than merely absent.
    if settings.oidc_base_url == Settings.model_fields["oidc_base_url"].default:
        logger.warning(
            "OIDC_BASE_URL is unset, so the development default %r is in force. "
            "That issuer exists on the celine-dev workspace and nowhere else: off "
            "it, its JWKS is unreachable and every /api/admin request will be "
            "denied. Set OIDC_BASE_URL to this deployment's realm.",
            settings.oidc_base_url,
        )

    # Same trade for email, with a softer failure: off this workspace nothing
    # answers on the dev Mailpit address, so every submission email is attempted,
    # fails and is logged — the submission itself is unaffected. Said once at boot
    # so a deployment learns it here rather than from a participant who never got
    # their confirmation.
    if settings.smtp_is_dev_default():
        logger.warning(
            "SMTP_HOST is unset, so the development default %s:%s (the workspace's "
            "Mailpit) is in force. Off celine-dev nothing answers there and no "
            "email is delivered. Set SMTP_HOST to this deployment's relay, or "
            "SMTP_HOST= to switch email off.",
            settings.smtp_host,
            settings.smtp_port,
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

    _warn_document_processing()

    # A real SMS gateway receives the participant's phone number, making it a
    # processor under GDPR Art. 28 exactly as the extraction provider is.
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
    _validate_provisioning_config()

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
        # **This is the OpenAPI document's version, and `celine-sdk` keys its
        # snapshot directory on it.** A change to this service's HTTP surface that
        # leaves this string alone overwrites `openapi/onboarding/v<version>/` in
        # place, so the same directory name comes to mean two different APIs and
        # no consumer can tell that anything moved — see the SDK's
        # `regenerating-clients` playbook, which treats that as a defect to
        # report. Moved to 0.2.0 for the member's self-service surface
        # (`/api/me/data-sharing`) and the `identity` block on its response.
        version="0.2.0",
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

    from celine.onboarding.api.admin import create_admin_router
    from celine.onboarding.api.config import router as config_router
    from celine.onboarding.api.consent_documents import router as consent_docs_router
    from celine.onboarding.api.documents import router as documents_router
    from celine.onboarding.api.downloads import router as downloads_router
    from celine.onboarding.api.eligibility import router as eligibility_router
    from celine.onboarding.api.extractions import router as extractions_router
    from celine.onboarding.api.health import router as health_router
    from celine.onboarding.api.me import router as me_router
    from celine.onboarding.api.phone_verify import router as phone_verify_router
    from celine.onboarding.api.recs import router as recs_router
    from celine.onboarding.api.submissions import router as submissions_router

    app.include_router(health_router, prefix="/api")
    app.include_router(recs_router, prefix="/api")
    app.include_router(downloads_router, prefix="/api")
    # Before the `{rec_slug}` block, not after: `/api/me/...` is a literal path
    # and `{rec_slug}` would match `me`. The admin router gets the opposite
    # treatment — last — because its own prefix is `/api/admin`, which no
    # `{rec_slug}` route can reach. `RESERVED_SLUGS` refuses a REC named `me` at
    # startup, so the collision cannot arrive from a manifest either.
    app.include_router(me_router, prefix="/api")

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
