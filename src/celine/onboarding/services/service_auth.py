"""The identities this service presents when it acts as itself.

There are two, and the split is not historical — they are granted by different
people for different things:

``svc-onboarding`` — ``OIDC_CLIENT_ID`` / ``OIDC_CLIENT_SECRET``
    celine's own client, and the one that asks for a participant's login.
    Provisioning a login in the celine realm is celine business, so the
    credential that does it is celine's.

``svc-ds-onboarding`` — ``DS_ONBOARDING_CLIENT_ID`` / ``DS_ONBOARDING_CLIENT_SECRET``
    the dataspace's client, carrying the grants a dataspace deployment gives
    this service: the identity registry, the connector, the registry lookups.

Keycloak provisioning used to be neither, and is now neither a Keycloak
credential at all. It logged in as a *person* — a realm administrator's username
and password, ``grant_type=password``, against the master realm — which meant
the service facing the public wizard held a credential that could do anything to
any realm, in order to create users in one. Then it presented ``svc-onboarding``
under a fine-grained grant over one realm group, which was the narrowest shape
Keycloak can express and still admin rights held by a public front door.

**It holds no Keycloak right now.** ``svc-onboarding`` carries the scope
``provisioning.participants.write``, and the login is created by
``../celine-policies``' provisioning service — one writer, reachable only from
inside the network. The token above is what that service checks the scope on;
nothing here reaches the Admin API. See ``services.provisioning``.

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


def celine_token_provider() -> OidcClientCredentialsProvider:
    """The celine-facing identity: this service's own client.

    Was ``keycloak_admin_token_provider``, and the rename is the point of the
    change that retired it. This token administers nothing — it carries
    ``provisioning.participants.write`` and is presented to the provisioning
    service, which is the thing that administers the realm. A name saying
    "keycloak admin" would keep describing a grant this client no longer has.
    """
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
