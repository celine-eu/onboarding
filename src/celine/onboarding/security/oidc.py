"""OIDC configuration for verifying operator and service tokens.

The onboarding service already had `OIDC_BASE_URL` for its *outbound* M2M calls.
User tokens arrive from the same issuer — oauth2-proxy authenticates against the
same realm every other CELINE console uses — so the issuer is shared rather than
configured twice.

Built here rather than as a nested `OidcSettings` field on `Settings` (grid's
shape) for one practical reason: a nested `BaseSettings` reads only the process
environment, so `CELINE_OIDC_*` in this repo's `.env` would be silently ignored.
Flat `OIDC_*` fields on `Settings` pick up `.env` like everything else.
"""

from __future__ import annotations

from functools import lru_cache

from celine.sdk.settings.models import OidcSettings

from celine.onboarding.config.settings import settings


def _derived_jwks_uri(base_url: str) -> str:
    return f"{base_url}/protocol/openid-connect/certs" if base_url else ""


@lru_cache(maxsize=1)
def oidc_settings() -> OidcSettings:
    """Issuer, JWKS and expected audience for inbound token verification.

    Cached: `JwtUser.from_token` keys its JWKS client cache on the URI string, so
    rebuilding this per request would be wasteful but not incorrect.
    """
    base_url = settings.oidc_base_url.strip().rstrip("/")
    return OidcSettings(
        base_url=base_url,
        jwks_uri=settings.oidc_jwks_uri.strip() or _derived_jwks_uri(base_url),
        # `None` disables audience verification in `JwtUser.from_token`. Leaving
        # the default set means a token minted for another service is rejected
        # here, which is the point of the audience mapper on oauth2-proxy.
        audience=settings.oidc_audience.strip() or None,
        client_id=settings.oidc_client_id.strip() or None,
        client_secret=settings.oidc_client_secret or None,
    )


def is_configured() -> bool:
    """Whether inbound tokens can be verified at all.

    Both are required: the issuer is checked against the `iss` claim, and without
    the JWKS there is no key to check the signature against.
    """
    oidc = oidc_settings()
    return bool(oidc.base_url and oidc.jwks_uri)
