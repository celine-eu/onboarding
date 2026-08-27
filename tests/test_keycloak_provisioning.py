"""Provisioning the login, and what the rest of enablement is told about it.

The Keycloak user is the first effect of approval, and the *username* it is
created or found under is the value the REC registry needs: every self-service
route there resolves a caller by matching `Member.user_id` against the token's
`preferred_username`. So what this module reports is not a detail — it decides
whether an approved participant can ever see their own membership (issue #1).
"""

from __future__ import annotations

import json

import httpx
import pytest

import celine.onboarding.services.keycloak_identity as ki

_OriginalAsyncClient = httpx.AsyncClient

TOKEN_RESPONSE = {"access_token": "admin-token"}


@pytest.fixture()
def _enabled(monkeypatch):
    monkeypatch.setattr(ki.settings, "dataspace_keycloak_enabled", True)
    monkeypatch.setattr(ki.settings, "dataspace_keycloak_base_url", "http://kc:8080")
    monkeypatch.setattr(ki.settings, "dataspace_keycloak_realm", "celine")
    monkeypatch.setattr(ki.settings, "dataspace_keycloak_admin_username", "admin")
    monkeypatch.setattr(ki.settings, "dataspace_keycloak_admin_password", "admin")
    monkeypatch.setattr(ki.settings, "dataspace_keycloak_default_password", "")
    monkeypatch.setattr(ki.settings, "dataspace_keycloak_update_existing", True)


def _patch_httpx(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def factory(**kw):
        kw.pop("transport", None)
        return _OriginalAsyncClient(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _handler(*, existing: list[dict] | None = None, seen: list[httpx.Request] | None = None):
    """Answer the admin API: a token, a lookup, and a creation."""

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json=TOKEN_RESPONSE)
        if request.method == "GET" and path.endswith("/users"):
            return httpx.Response(200, json=existing or [])
        if request.method == "POST" and path.endswith("/users"):
            return httpx.Response(
                201, headers={"Location": "http://kc:8080/admin/realms/celine/users/kc-new"}
            )
        if request.method == "PUT":
            return httpx.Response(204)
        raise AssertionError(f"unexpected call {request.method} {request.url}")

    return handle


class TestTheUsernameReported:
    async def test_a_created_user_is_named_by_their_email(self, monkeypatch, submission, _enabled):
        _patch_httpx(monkeypatch, _handler())

        result = await ki.provision_keycloak_user(submission)

        assert result.created is True
        assert result.user_id == "kc-new"
        assert result.username == "user@example.com"

    async def test_an_existing_user_keeps_the_username_keycloak_holds(
        self, monkeypatch, submission, _enabled
    ):
        """Not the email we looked them up by. The second lookup matches on the
        email, so a user created by anything else comes back under a name of its
        choosing — and that is what their token will carry. Reporting the email
        would hand the registry a `user_id` as unresolvable as the one this
        fixed."""
        _patch_httpx(monkeypatch, _handler(existing=[{"id": "kc-1", "username": "gl-00001"}]))

        result = await ki.provision_keycloak_user(submission)

        assert result.created is False
        assert result.username == "gl-00001"

    async def test_a_user_with_no_username_falls_back_to_the_email(
        self, monkeypatch, submission, _enabled
    ):
        _patch_httpx(monkeypatch, _handler(existing=[{"id": "kc-1"}]))

        result = await ki.provision_keycloak_user(submission)

        assert result.username == "user@example.com"


class TestAdoptingAnExistingUser:
    async def test_the_update_does_not_rename_them(self, monkeypatch, submission, _enabled):
        """Renaming a login is not what "update existing" is for: it changes what
        the person types to sign in, and invalidates the `user_id` any registry
        row already holds for them."""
        seen: list[httpx.Request] = []
        _patch_httpx(
            monkeypatch,
            _handler(existing=[{"id": "kc-1", "username": "gl-00001"}], seen=seen),
        )

        await ki.provision_keycloak_user(submission)

        puts = [r for r in seen if r.method == "PUT"]
        assert puts, "the existing user should still have their profile refreshed"
        assert "username" not in json.loads(puts[0].content)

    async def test_the_profile_is_still_refreshed(self, monkeypatch, submission, _enabled):
        seen: list[httpx.Request] = []
        _patch_httpx(
            monkeypatch,
            _handler(existing=[{"id": "kc-1", "username": "gl-00001"}], seen=seen),
        )

        await ki.provision_keycloak_user(submission)

        body = json.loads([r for r in seen if r.method == "PUT"][0].content)
        assert body["email"] == "user@example.com"
        assert body["firstName"] == "Alice"


class TestTheUsernameThisServiceWouldCreate:
    """The fallback for anything that cannot have the observed value: a retry of
    registration alone, or a deployment that provisions no Keycloak user."""

    def test_it_is_the_normalised_email(self, submission):
        submission.email = "  Alice.Rossi@Example.org "
        assert ki.keycloak_username(submission) == "alice.rossi@example.org"

    def test_no_email_means_no_username(self, submission):
        submission.email = None
        assert ki.keycloak_username(submission) is None


class TestDisabled:
    async def test_nothing_is_provisioned(self, monkeypatch, submission):
        monkeypatch.setattr(ki.settings, "dataspace_keycloak_enabled", False)
        assert await ki.provision_keycloak_user(submission) is None
