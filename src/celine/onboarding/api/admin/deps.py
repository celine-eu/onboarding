"""Dependencies for the admin console surface.

Every endpoint under `/api/admin` names the capability it needs, and the
capability is checked against the *community in the path* — so the same operator
is allowed on their own REC and refused on somebody else's. See
`policies/celine/onboarding/access.rego` for what grants what.
"""

from __future__ import annotations

import logging
from typing import Annotated

import jwt as pyjwt
from celine.sdk.auth import JwtUser
from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.config.settings import settings
from celine.onboarding.models.database import get_db
from celine.onboarding.security.oidc import is_configured, oidc_settings
from celine.onboarding.security.policy import Capability, get_policy
from celine.onboarding.services import template_service
from celine.onboarding.services.audit_service import Actor

logger = logging.getLogger(__name__)

# Path segments the admin router uses literally, which therefore cannot also be a
# REC slug at this prefix. Checked at startup so the collision is a boot failure
# rather than a REC that mysteriously 404s.
RESERVED_SLUGS = frozenset({"recs", "me", "ping"})


def _extract_token(request: Request) -> str | None:
    """oauth2-proxy's header first, then `Authorization: Bearer`.

    Same order as celine-webapp and celine-grid. Both are *untrusted* input: the
    signature is verified against the issuer's JWKS either way, and this service
    must never grow a "trust the proxy header" mode — the public wizard shares the
    process, so anything reachable at `/api/*` is reachable unauthenticated.
    """
    token = request.headers.get(settings.jwt_header_name)
    if token:
        return token

    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def get_current_user(request: Request) -> JwtUser:
    if not is_configured():
        # Startup refuses this, so reaching it means the configuration changed
        # under a running process. Fail closed and say why.
        logger.error("Admin request rejected: OIDC is not configured")
        raise HTTPException(503, "Admin access is not configured")

    token = _extract_token(request)
    if not token:
        raise HTTPException(401, "Missing authentication token")

    try:
        return JwtUser.from_token(token, oidc=oidc_settings())
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(401, "Token has expired")
    except pyjwt.InvalidTokenError as exc:
        raise HTTPException(401, f"Invalid token: {exc}")
    except Exception as exc:
        logger.warning("Token verification failed: %s", exc)
        raise HTTPException(401, "Authentication failed")


async def valid_admin_rec(rec_slug: str) -> str:
    """Resolve a REC slug in the path, 404 if the deployment does not serve it."""
    await template_service.ensure_fresh()
    if rec_slug not in template_service.get_slugs():
        raise HTTPException(404, f"REC '{rec_slug}' not found")
    return rec_slug


def organization_of(rec_slug: str) -> str | None:
    """The Keycloak organization owning *rec_slug*, or None if it declares none.

    None is not an error: such a REC is administrable only by platform operators
    holding a realm-level group, and the policy resolves that correctly.
    """
    return template_service.organization_for(rec_slug) or None


def require(capability: Capability):
    """A dependency enforcing *capability* on the REC named in the path."""

    async def _dependency(
        rec_slug: Annotated[str, Depends(valid_admin_rec)],
        user: Annotated[JwtUser, Depends(get_current_user)],
    ) -> JwtUser:
        decision = get_policy().allow(user, capability, organization=organization_of(rec_slug))
        if not decision.allowed:
            raise HTTPException(403, decision.reason or "access denied")
        return user

    return _dependency


def require_global(capability: Capability):
    """A dependency for deployment-wide actions, which belong to no community.

    Only a *realm*-level group satisfies these for an operator — an
    organization-scoped grant has no community to match against. Service accounts
    are unaffected: they are authorised by scope and never by organization.
    """

    async def _dependency(
        user: Annotated[JwtUser, Depends(get_current_user)],
    ) -> JwtUser:
        decision = get_policy().allow(user, capability, organization=None)
        if not decision.allowed:
            raise HTTPException(403, decision.reason or "access denied")
        return user

    return _dependency


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


def current_actor(user: Annotated[JwtUser, Depends(get_current_user)]) -> Actor:
    return Actor.from_user(user)


UserDep = Annotated[JwtUser, Depends(get_current_user)]
DbDep = Annotated[AsyncSession, Depends(get_db)]
RecDep = Annotated[str, Depends(valid_admin_rec)]
ActorDep = Annotated[Actor, Depends(current_actor)]
IpDep = Annotated[str, Depends(client_ip)]
