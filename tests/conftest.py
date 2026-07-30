from __future__ import annotations

import os
import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _minimal_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


@pytest.fixture(autouse=True)
def _isolate_manifest_cache():
    """Keep the REC manifest cache from leaking between tests.

    The cache is module-level and TTL-refreshed from the database, so a test that
    seeds it would otherwise decide what a later test resolves.
    """
    from celine.onboarding.services import template_service

    saved = dict(template_service._cache)
    saved_at = template_service._cache_loaded_at
    yield
    template_service._cache.clear()
    template_service._cache.update(saved)
    template_service._cache_loaded_at = saved_at


@pytest.fixture()
def bind_rec(monkeypatch):
    """Seed a REC manifest so `dataspace_binding` resolves without a database.

    Returns a callable so a test can bind several communities and assert they do
    not bleed into one another — the multi-tenancy property the manifest binding
    exists to provide.
    """
    from celine.onboarding.services import template_service

    async def _noop_fresh():
        return None

    monkeypatch.setattr(template_service, "ensure_fresh", _noop_fresh)

    def _bind(rec_slug: str = "default", **dataspace):
        manifest: dict = {"slug": rec_slug, "name": rec_slug}
        if dataspace:
            manifest["dataspace"] = dataspace
        template_service._cache[rec_slug] = manifest
        return manifest

    return _bind


@pytest.fixture(scope="session")
def _signing_key():
    """One RSA keypair for the whole session — generation is the slow part."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def issue_token(monkeypatch, _signing_key):
    """Mint JWTs the app verifies for real, against a test key.

    Signature, issuer, audience and expiry are all checked by the same
    `JwtUser.from_token` path production uses — only the key source is swapped, by
    replacing the SDK's cached JWKS client. Stubbing verification instead would
    leave the one thing most worth testing untested.
    """
    import jwt as pyjwt

    from celine.onboarding.config.settings import settings
    from celine.onboarding.security import oidc

    issuer = "https://keycloak.test/realms/celine"
    monkeypatch.setattr(settings, "oidc_base_url", issuer)
    monkeypatch.setattr(settings, "oidc_jwks_uri", f"{issuer}/protocol/openid-connect/certs")
    monkeypatch.setattr(settings, "oidc_audience", "svc-onboarding")
    oidc.oidc_settings.cache_clear()

    class _FakeSigningKey:
        key = _signing_key.public_key()

    class _FakeJwksClient:
        def get_signing_key_from_jwt(self, token):
            return _FakeSigningKey()

    from celine.sdk.auth import jwt as sdk_jwt

    monkeypatch.setattr(sdk_jwt, "_get_jwks_client", lambda uri: _FakeJwksClient())

    def _issue(**claims) -> str:
        payload = {
            "iss": issuer,
            "aud": "svc-onboarding",
            "sub": "test-subject",
            "exp": int(time.time()) + 300,
            "iat": int(time.time()),
            **claims,
        }
        return pyjwt.encode(payload, _signing_key, algorithm="RS256")

    yield _issue
    oidc.oidc_settings.cache_clear()


@pytest.fixture()
def operator_token(issue_token):
    """A REC operator: an organization membership plus a group inside it."""

    def _issue(organization: str, *groups: str, realm: tuple[str, ...] = (), **extra):
        claims: dict = {
            "sub": "operator-sub",
            "email": "operator@example.org",
            "preferred_username": "operator",
            "organization": {
                organization: {"id": "org-uuid", "groups": [f"/{g}" for g in groups]}
            },
            **extra,
        }
        if realm:
            claims["groups"] = [f"/{g}" for g in realm]
        return issue_token(**claims)

    return _issue


@pytest.fixture()
def service_token(issue_token):
    """A client_credentials token: scopes, no organization, no groups."""

    def _issue(*scopes: str, client_id: str = "svc-onboarding-cli"):
        return issue_token(
            sub=f"service-account-{client_id}",
            preferred_username=f"service-account-{client_id}",
            client_id=client_id,
            azp=client_id,
            scope=" ".join(scopes),
        )

    return _issue


@pytest.fixture()
def seed_rec(monkeypatch):
    """Seed an arbitrary REC manifest into the cache, no database involved.

    `bind_rec` only reaches the `dataspace:` block; this one takes any top-level
    manifest key, which the admin console needs (`organization:`).
    """
    from celine.onboarding.services import template_service

    async def _noop_fresh():
        return None

    monkeypatch.setattr(template_service, "ensure_fresh", _noop_fresh)

    def _seed(rec_slug: str = "default", **manifest):
        full: dict = {"slug": rec_slug, "name": rec_slug, **manifest}
        template_service._cache[rec_slug] = full
        return full

    return _seed


@pytest.fixture()
def submission():
    sub = MagicMock()
    sub.ref = "20260713-abcd1234"
    sub.email = "user@example.com"
    sub.first_name = "Alice"
    sub.last_name = "Test"
    sub.rec_slug = "default"
    sub.dataspace_vc_id = None
    sub.dataspace_subject_id = None
    sub.dataspace_did = None
    sub.dataspace_vc_issued_at = None
    return sub
