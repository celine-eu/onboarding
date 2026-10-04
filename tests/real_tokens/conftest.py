"""Real-token checks: the admin surface against tokens a running Keycloak issues.

The unit suite mints its own tokens, so it proves the policy against the claim shapes
somebody wrote down. These checks ask a real realm for them and let the app verify
them for real, against the realm's JWKS — the only way to learn that the shapes a
realm actually issues still mean what the policy thinks they mean (REQ-0030).

**Everything is opt-in and nothing has a default.** A default would have to name one
particular realm, its users and its secrets, and the suite would then report green
against a stack the reader cannot see. Unset, every check skips and says why;
`task test:real-tokens` runs pytest with `-rs` so the reasons are printed.

  ONBOARDING_KC_TOKEN_URL       the realm's token endpoint; the issuer is derived from it
  ONBOARDING_KC_CLIENT_ID       the client people's tokens are issued to (direct grant on)
  ONBOARDING_KC_CLIENT_SECRET   its secret
  ONBOARDING_KC_SCOPE           optional; requested scope, e.g. "openid organization:*"
  ONBOARDING_KC_ORGANIZATION    the alias of an organization typed `rec` the users below
                                belong to
  ONBOARDING_KC_PLATFORM_ADMIN  "user:password" of a holder of the `platform-admin` role
  ONBOARDING_KC_ORG_ADMIN       "user:password" of that organization's `admins`, holding
                                no platform role
  ONBOARDING_KC_ORG_VIEWER      optional; "user:password" of its `viewers`
  ONBOARDING_KC_LEGACY_TOKEN    optional; an access token that still carries a realm
                                `/admins` group (a realm today emits none, so it has to
                                be minted by a fixture outside this repository)
  ONBOARDING_KC_SERVICE_CLIENT  optional; "client:secret" of a client-credentials client
                                holding `onboarding.admin`
"""

from __future__ import annotations

import os

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from celine.onboarding.api.admin import create_admin_router
from celine.onboarding.security.middleware import AdminAuthMiddleware

TOKEN_SUFFIX = "/protocol/openid-connect/token"


def _env(name: str) -> str | None:
    """The variable, or None when it is unset or blank."""
    return os.environ.get(name, "").strip() or None


def _require(name: str) -> str:
    value = _env(name)
    if value is None:
        pytest.skip(
            f"{name} is not set: no real Keycloak to ask (see tests/real_tokens/conftest.py)"
        )
    return value


@pytest.fixture(scope="session")
def token_url() -> str:
    url = _require("ONBOARDING_KC_TOKEN_URL")
    if not url.endswith(TOKEN_SUFFIX):
        pytest.fail(f"ONBOARDING_KC_TOKEN_URL must end with {TOKEN_SUFFIX}: {url}")
    return url


@pytest.fixture(scope="session")
def issuer(token_url) -> str:
    return token_url[: -len(TOKEN_SUFFIX)]


@pytest.fixture(scope="session")
def organization() -> str:
    return _require("ONBOARDING_KC_ORGANIZATION")


@pytest.fixture(scope="session")
def user_token(token_url):
    """A person's access token, by password grant on the configured client."""
    client_id = _require("ONBOARDING_KC_CLIENT_ID")
    secret = _require("ONBOARDING_KC_CLIENT_SECRET")
    scope = _env("ONBOARDING_KC_SCOPE")

    def _mint(variable: str) -> str:
        user, _, password = _require(variable).partition(":")
        data = {
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": secret,
            "username": user,
            "password": password,
        }
        if scope:
            data["scope"] = scope
        try:
            response = httpx.post(token_url, data=data, timeout=10)
        except httpx.HTTPError as exc:
            pytest.skip(f"Keycloak at {token_url} is unreachable: {exc}")
        assert response.status_code == 200, f"{variable}: {response.text}"
        return response.json()["access_token"]

    return _mint


@pytest.fixture(scope="session")
def legacy_token() -> str:
    return _require("ONBOARDING_KC_LEGACY_TOKEN")


@pytest.fixture(scope="session")
def service_access_token(token_url) -> str:
    client_id, _, secret = _require("ONBOARDING_KC_SERVICE_CLIENT").partition(":")
    response = httpx.post(
        token_url,
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret},
        timeout=10,
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture()
def client(monkeypatch, issuer, organization, seed_rec) -> TestClient:
    """The admin surface, verifying tokens against the real realm's JWKS.

    Three communities: one owned by the configured organization, one by an
    organization nobody here belongs to, and one that names none.
    """
    from celine.onboarding.config.settings import settings
    from celine.onboarding.security import oidc

    monkeypatch.setattr(settings, "oidc_base_url", issuer)
    monkeypatch.setattr(settings, "oidc_jwks_uri", "")
    monkeypatch.setattr(settings, "oidc_audience", "svc-onboarding")
    oidc.oidc_settings.cache_clear()

    seed_rec("own-rec", name="Own REC", organization=organization)
    seed_rec("foreign-rec", name="Foreign REC", organization="kc-check-nobodys-organization")
    seed_rec("unbound-rec", name="Unbound REC")

    app = FastAPI()
    app.add_middleware(AdminAuthMiddleware)
    app.include_router(create_admin_router())
    yield TestClient(app)
    oidc.oidc_settings.cache_clear()
