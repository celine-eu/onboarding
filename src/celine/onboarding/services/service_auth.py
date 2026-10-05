"""The identities this service presents when it acts as itself, or for a community.

There are three, and the split is not historical — they are granted by different
people for different things:

``svc-onboarding`` — ``OIDC_CLIENT_ID`` / ``OIDC_CLIENT_SECRET``
    celine's own client, and the one that asks for a participant's login.
    Provisioning a login in the celine realm is celine business, so the
    credential that does it is celine's.

``svc-ds-onboarding`` — ``DS_ONBOARDING_CLIENT_ID`` / ``DS_ONBOARDING_CLIENT_SECRET``
    the dataspace's client, for what this service does as itself there: resolve
    an organisation, resolve a subject, write the DID <-> Keycloak mapping.

``svc-ds-collector-<alias>`` — derived per REC / ``SVC_DS_COLLECTOR_<ALIAS>_SECRET``
    **a community's own collector client, not this service's.** Every act done
    *for* a community — its members' memberships and credentials, their consent
    registrations and read-backs, the audience read — is that organisation's act,
    and the receiver binds it to the organisation the token's ``sub`` names. One
    secret per community, so a deployment serving several holds several, and no
    secret is shared across onboarding operators. See :func:`collector_token_provider`.
    **Each token asks for exactly one scope** (:data:`COLLECTOR_SCOPES`): each
    scope adds exactly one audience, and a receiver refuses a collector token
    naming another service's audience, so a token replayed elsewhere is useless.

``svc-ds-connector-<alias>`` — ``DS_ORG_CLIENT_SECRET``, **development transition**
    the community's connector client, which the consent calls used before ds had
    a collector client. Only under ``CELINE_ENV=dev``, only for a community with
    no collector secret, and with a warning (:func:`organisation_token_provider`).

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

import logging
import os

from celine.sdk.auth import OidcClientCredentialsProvider
from celine.sdk.posture import PostureGuard, is_dev

from celine.onboarding.config.settings import (
    COLLECTOR_SECRET_PREFIX,
    COLLECTOR_SECRET_SUFFIX,
    settings,
)
from celine.onboarding.services.errors import ConfigurationError

logger = logging.getLogger(__name__)

_providers: dict[tuple[str, str | None], OidcClientCredentialsProvider] = {}


def _provider_for(
    client_id: str, client_secret: str, *, scope: str | None = None
) -> OidcClientCredentialsProvider:
    """One provider per client **and per requested optional scope**.

    A token asked for with ``scope`` carries that optional scope on top of the
    client's defaults, so it is cached apart from the default token: a call that
    needs no write must never be handed the token of one that did.
    """
    if not settings.oidc_base_url:
        raise ConfigurationError(
            "OIDC_BASE_URL is required for any call this service makes as itself"
        )
    key = (client_id, scope)
    if key not in _providers:
        _providers[key] = OidcClientCredentialsProvider(
            base_url=settings.oidc_base_url,
            client_id=client_id,
            client_secret=client_secret,
            scope=scope,
        )
    return _providers[key]


def service_token_provider() -> OidcClientCredentialsProvider:
    """The dataspace-facing identity: the identity registry and the connector."""
    return _provider_for(settings.ds_onboarding_client_id, settings.ds_onboarding_client_secret)


def registry_token_provider() -> OidcClientCredentialsProvider:
    """The identity the REC registry member client presents (REQ-0022).

    ``svc-onboarding`` when the dataspace is disabled, ``svc-ds-onboarding``
    only when ``DATASPACE_ENABLED`` (D60). Both are granted
    ``rec-registry.members.write`` and ``rec-registry.lookup``; the dataspace
    client exists only on a realm that runs the dataspace, so a deployment
    without one registers members as celine's own client instead of failing
    approval with a 401 from the token endpoint.
    """
    if settings.dataspace_enabled:
        return service_token_provider()
    return celine_token_provider()


def organisation_client_id(organization_alias: str) -> str:
    """The client a community registers consent as: ``svc-ds-connector-<alias>``.

    ds's own naming — the client ``ir-cli keycloak org-sync`` and the
    provisioning bundle create beside the participant — so deriving it here reads
    a convention rather than inventing one. ``DS_ORG_CLIENT_ID`` overrides it for
    a deployment whose client is named otherwise.
    """
    if settings.ds_org_client_id.strip():
        return settings.ds_org_client_id.strip()
    alias = organization_alias.strip()
    if not alias:
        raise ConfigurationError(
            "No dataspace organisation alias, so the organisation client that "
            "registers consent cannot be named. Set the REC manifest's "
            "'dataspace.organization', or DS_ORG_CLIENT_ID."
        )
    return f"svc-ds-connector-{alias}"


def organisation_token_provider(organization_alias: str) -> OidcClientCredentialsProvider:
    """**Development transition only**: the community's *connector* client.

    What the consent calls (``POST /consent/admin/shares``, ``GET
    /consent/admin/subject-shares``, ``GET /consent/admin/decisions``) were made
    as before ds had a collector client, and what they are still made as under
    ``CELINE_ENV=dev`` for a community whose collector secret is not set — a
    realm and a ds that predate the collector client. Never otherwise: see
    :func:`collector_token_provider`.
    """
    client_id = organisation_client_id(organization_alias)
    if not settings.ds_org_client_secret:
        raise ConfigurationError(
            f"DS_ORG_CLIENT_SECRET is not set, so this service cannot "
            f"authenticate as {client_id} — and only an organisation's own "
            "client may register a consent. A service token is refused (403)."
        )
    return _provider_for(client_id, settings.ds_org_client_secret)


async def organisation_auth_headers(organization_alias: str) -> dict[str, str]:
    """``Authorization`` as the community's connector client (development transition)."""
    token = await organisation_token_provider(organization_alias).get_token()
    return {"Authorization": f"Bearer {token.access_token}"}


# --- the community's collector client ---------------------------------------

#: ``POST /admin/memberships``, ``DELETE /admin/memberships/{did}/{org}`` (IR).
MEMBERSHIPS_WRITE = "identity-registry.memberships.write"
#: ``POST /admin/credentials/data-subject`` (and ``/transition``),
#: ``DELETE /admin/credentials/{id}`` (IR).
CREDENTIALS_WRITE = "identity-registry.credentials.write"
#: ``POST /consent/admin/shares``, ``POST /consent/request`` (connector).
CONSENT_PROVISION = "connector.consent.provision"
#: ``GET /consent/admin/subject-shares``, ``GET /consent/admin/decisions`` (connector).
CONSENT_COLLECTOR_READ = "connector.consent.collector.read"
#: ``GET /consent/admin/shares`` — the audience read (connector).
CONSENT_AUDIENCE = "connector.consent.audience"
#: ``POST /admin/disclosure`` (connector). No call site today (ADR-0010).
DISCLOSURE_RECORD = "connector.disclosure.record"
#: provenance write routes. No call site today: this service only reads, as the member.
PROVENANCE_WRITE = "provenance.write"

#: Every scope a collector token may ask for — **one at a time**. A token is
#: requested with exactly one of these and nothing else (ds collector contract
#: v1): each adds exactly one audience, and a receiver refuses a collector token
#: that names another ds service's audience.
COLLECTOR_SCOPES: frozenset[str] = frozenset(
    {
        MEMBERSHIPS_WRITE,
        CREDENTIALS_WRITE,
        CONSENT_PROVISION,
        CONSENT_COLLECTOR_READ,
        CONSENT_AUDIENCE,
        DISCLOSURE_RECORD,
        PROVENANCE_WRITE,
    }
)

#: The calls that, before the collector client, were made as the community's
#: **connector** client; every other collector scope's call was made as
#: ``svc-ds-onboarding``. What the development transition falls back to.
_TRANSITION_CONNECTOR_CLIENT_SCOPES = frozenset({CONSENT_PROVISION, CONSENT_COLLECTOR_READ})

_transition_warned: set[tuple[str, str]] = set()


def collector_client_id(organization_alias: str) -> str:
    """``svc-ds-collector-<alias>``: ds's name for the community's collector client."""
    alias = organization_alias.strip()
    if not alias:
        raise ConfigurationError(
            "No dataspace organisation alias, so the community's collector client "
            "cannot be named. Set the REC manifest's 'dataspace.organization'."
        )
    return f"svc-ds-collector-{alias}"


def collector_secret_env(organization_alias: str) -> str:
    """``SVC_DS_COLLECTOR_<ALIAS>_SECRET``: alias upper-cased, ``-`` to ``_``."""
    alias = organization_alias.strip().upper().replace("-", "_")
    return f"{COLLECTOR_SECRET_PREFIX}{alias}{COLLECTOR_SECRET_SUFFIX}"


def collector_secret(organization_alias: str) -> str:
    """The community's collector secret, or ``""`` when none is configured.

    The process environment first, then a dotenv file (``Settings``), the same
    precedence every other setting has.
    """
    if not organization_alias.strip():
        return ""
    name = collector_secret_env(organization_alias)
    value = os.environ.get(name)
    if value is None:
        value = settings.ds_collector_secrets.get(name, "")
    return value.strip()


def collector_token_provider(
    organization_alias: str, scope: str
) -> OidcClientCredentialsProvider | None:
    """The community's collector client, asking for ``scope`` and nothing else.

    One provider — so one cached token — per ``(alias, scope)``. Returns ``None``
    only for the **development transition**: under ``CELINE_ENV=dev``, a
    community whose collector secret is not set keeps today's client (warned
    once per alias and scope), because a local realm and ds that predate the
    collector client have none to give. Anywhere else a missing secret raises
    :class:`ConfigurationError` — which boot has already refused
    (:func:`collector_posture_guard`), so this is the backstop.
    """
    if scope not in COLLECTOR_SCOPES:
        # Exactly one known scope, never a space-joined list: a multi-scope
        # token carries several audiences and every receiver refuses it.
        raise ValueError(f"not a single collector scope: {scope!r}")
    secret = collector_secret(organization_alias)
    if secret:
        return _provider_for(collector_client_id(organization_alias), secret, scope=scope)
    if not is_dev():
        raise ConfigurationError(
            f"{collector_secret_env(organization_alias or '<alias>')} is not set, so this "
            f"service cannot act for {organization_alias or 'a community with no alias'!r} "
            "in the dataspace: every such act is the community's own, made as "
            "svc-ds-collector-<alias>. Only CELINE_ENV=dev falls back to the "
            "pre-collector clients."
        )
    key = (organization_alias, scope)
    if key not in _transition_warned:
        _transition_warned.add(key)
        legacy = (
            "svc-ds-connector-<alias> (DS_ORG_CLIENT_SECRET)"
            if scope in _TRANSITION_CONNECTOR_CLIENT_SCOPES
            else settings.ds_onboarding_client_id
        )
        logger.warning(
            "%s is not set: acting for %r as %s for %s calls, the pre-collector "
            "client. Development only (CELINE_ENV=dev); a ds and realm with "
            "collector clients refuse it.",
            collector_secret_env(organization_alias or "<alias>"),
            organization_alias,
            legacy,
            scope,
        )
    return None


def uses_connector_client_in_transition(scope: str) -> bool:
    """Whether the development transition makes ``scope``'s call as the connector client.

    The rest were ``svc-ds-onboarding``'s, and fall back to it.
    """
    return scope in _TRANSITION_CONNECTOR_CLIENT_SCOPES


def collecting_organisations() -> dict[str, list[str]]:
    """Every dataspace organisation this instance acts for, with the RECs bound to it.

    A community bound to a dataspace organisation, in a deployment with the
    dataspace on, is one this service writes memberships, credentials and
    consent for — so each needs its collector client. Several RECs may share one
    organisation, and then share its secret.
    """
    from celine.onboarding.services import template_service

    if not settings.dataspace_enabled:
        return {}
    out: dict[str, list[str]] = {}
    for slug in template_service.get_slugs():
        binding = template_service.dataspace_binding(slug)
        if binding.enabled:
            out.setdefault(binding.organization, []).append(slug)
    return out


def collector_posture_guard(env: str | None = None) -> PostureGuard:
    """A posture guard holding every bound community's missing or weak collector secret.

    Run after the manifests are loaded (they are what names the communities).
    ``env`` overrides the environment signal, for tests.
    """
    guard = PostureGuard("onboarding", env=env)
    for alias, slugs in sorted(collecting_organisations().items()):
        name = collector_secret_env(alias)
        client_id = collector_client_id(alias)
        secret = collector_secret(alias)
        recs = ", ".join(repr(s) for s in slugs)
        if not secret:
            guard.add(
                name,
                f"is not set, and REC {recs} acts for dataspace organisation {alias!r} "
                f"as {client_id}",
                f"Set {name} to {client_id}'s secret (ds provisions the client for "
                "an organisation that collects consent), or remove the REC's "
                "'dataspace' block.",
            )
        else:
            guard.forbid_secret_equal_to_client_id(name, client_id, secret)
    return guard


def enforce_collector_posture(env: str | None = None) -> None:
    """Refuse to start outside dev with a bound community lacking its collector secret.

    Warns in dev, where the transition (:func:`collector_token_provider`)
    falls back to the pre-collector clients instead.
    """
    collector_posture_guard(env=env).enforce()


def celine_token_provider(scope: str | None = None) -> OidcClientCredentialsProvider:
    """The celine-facing identity: this service's own client.

    Was ``keycloak_admin_token_provider``, and the rename is the point of the
    change that retired it. This token administers nothing — it carries
    ``provisioning.participants.write`` and is presented to the provisioning
    service, which is the thing that administers the realm. A name saying
    "keycloak admin" would keep describing a grant this client no longer has.

    ``scope`` asks for one of the client's **optional** scopes on top of its
    defaults, for the one call that needs it: ``rec-registry.community.write``
    for the registry sync's writes and ``provisioning.reconcile`` for its set-up
    step. Neither is in the default token, so nothing else this service does
    carries them.
    """
    if scope is None:
        return _provider_for(settings.oidc_client_id, settings.oidc_client_secret)
    return _provider_for(settings.oidc_client_id, settings.oidc_client_secret, scope=scope)


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
    _transition_warned.clear()


async def service_auth_headers() -> dict[str, str]:
    token = await service_token_provider().get_token()
    return {"Authorization": f"Bearer {token.access_token}"}
