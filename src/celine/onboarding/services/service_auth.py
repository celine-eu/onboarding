"""The one credential this service uses to act as itself.

Everything this app calls outbound authenticates as the same OIDC service
account — `DS_ONBOARDING_CLIENT_ID` against `OIDC_BASE_URL`. Two callers today:
the dataspace identity registry and connector, and the Keycloak Admin API that
provisions a participant's login on approval.

Keycloak provisioning used to be the exception. It logged in as a *person* — a
realm administrator's username and password, `grant_type=password`, against the
master realm — which meant an internet-facing service held a credential that
could do anything to any realm, to create users in one. It now presents this
token like every other call, and the two roles it actually needs
(`manage-users`, `view-users`) are granted to the service account in Keycloak.

The provider caches and renews its own token, so it is built once per process.
"""

from __future__ import annotations

from celine.sdk.auth import OidcClientCredentialsProvider

from celine.onboarding.config.settings import settings
from celine.onboarding.services.errors import ConfigurationError

_provider: OidcClientCredentialsProvider | None = None


def service_token_provider() -> OidcClientCredentialsProvider:
    """The shared client-credentials provider, built on first use."""
    global _provider
    if _provider is None:
        if not settings.oidc_base_url:
            raise ConfigurationError(
                "OIDC_BASE_URL is required for any call this service makes as itself"
            )
        _provider = OidcClientCredentialsProvider(
            base_url=settings.oidc_base_url,
            client_id=settings.ds_onboarding_client_id,
            client_secret=settings.ds_onboarding_client_secret,
        )
    return _provider


def issuer_realm(oidc_base_url: str) -> str | None:
    """The realm an issuer URL names, or ``None`` when it names none.

    `.../realms/celine` is the shape every Keycloak issuer has; anything else is
    another OIDC provider, whose tokens administer no Keycloak realm at all.
    """
    parts = [p for p in oidc_base_url.strip().rstrip("/").split("/") if p]
    if len(parts) >= 2 and parts[-2] == "realms":
        return parts[-1]
    return None


def reset_service_token_provider() -> None:
    """Drop the cached provider. For tests, and for a settings change at boot."""
    global _provider
    _provider = None


async def service_auth_headers() -> dict[str, str]:
    token = await service_token_provider().get_token()
    return {"Authorization": f"Bearer {token.access_token}"}
