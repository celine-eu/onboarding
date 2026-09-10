"""The identities this service presents when it acts as itself.

There are two, and the split is not historical — they are granted by different
people for different things:

``svc-onboarding`` — ``OIDC_CLIENT_ID`` / ``OIDC_CLIENT_SECRET``
    celine's own client, and the one that administers users in the realm.
    Provisioning a participant's login is celine business, so the credential
    that does it is celine's.

``svc-ds-onboarding`` — ``DS_ONBOARDING_CLIENT_ID`` / ``DS_ONBOARDING_CLIENT_SECRET``
    the dataspace's client, carrying the grants a dataspace deployment gives
    this service: the identity registry, the connector, the registry lookups.

Keycloak provisioning used to be neither. It logged in as a *person* — a realm
administrator's username and password, ``grant_type=password``, against the
master realm — which meant the service facing the public wizard held a
credential that could do anything to any realm, in order to create users in one.
It now presents ``svc-onboarding``, whose service account is granted the members
of one realm group — ``DATASPACE_KEYCLOAK_PARTICIPANTS_GROUP`` — and nothing else
in the realm. Not even the realm's user search: see ``keycloak_identity``.

Providers cache and renew their own tokens, so one is built per client id per
process.
"""

from __future__ import annotations

from celine.sdk.auth import OidcClientCredentialsProvider

from celine.onboarding.config.settings import settings
from celine.onboarding.services.errors import ConfigurationError

_providers: dict[str, OidcClientCredentialsProvider] = {}


def _provider_for(client_id: str, client_secret: str) -> OidcClientCredentialsProvider:
    if not settings.oidc_base_url:
        raise ConfigurationError(
            "OIDC_BASE_URL is required for any call this service makes as itself"
        )
    if client_id not in _providers:
        _providers[client_id] = OidcClientCredentialsProvider(
            base_url=settings.oidc_base_url,
            client_id=client_id,
            client_secret=client_secret,
        )
    return _providers[client_id]


def service_token_provider() -> OidcClientCredentialsProvider:
    """The dataspace-facing identity: the identity registry and the connector."""
    return _provider_for(settings.ds_onboarding_client_id, settings.ds_onboarding_client_secret)


def keycloak_admin_token_provider() -> OidcClientCredentialsProvider:
    """The realm-facing identity: this service's own client, administering users."""
    return _provider_for(settings.oidc_client_id, settings.oidc_client_secret)


def issuer_realm(oidc_base_url: str) -> str | None:
    """The realm an issuer URL names, or ``None`` when it names none.

    `.../realms/celine` is the shape every Keycloak issuer has; anything else is
    another OIDC provider, whose tokens administer no Keycloak realm at all.
    """
    parts = [p for p in oidc_base_url.strip().rstrip("/").split("/") if p]
    if len(parts) >= 2 and parts[-2] == "realms":
        return parts[-1]
    return None


def reset_token_providers() -> None:
    """Drop the cached providers. For tests, and for a settings change at boot."""
    _providers.clear()


async def service_auth_headers() -> dict[str, str]:
    token = await service_token_provider().get_token()
    return {"Authorization": f"Bearer {token.access_token}"}


async def keycloak_admin_auth_headers() -> dict[str, str]:
    token = await keycloak_admin_token_provider().get_token()
    return {"Authorization": f"Bearer {token.access_token}"}
