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
from celine.onboarding.services.errors import ConfigurationError

_OriginalAsyncClient = httpx.AsyncClient

SERVICE_TOKEN = "service-account-token"


@pytest.fixture()
def _enabled(monkeypatch):
    monkeypatch.setattr(ki.settings, "dataspace_keycloak_enabled", True)
    monkeypatch.setattr(ki.settings, "dataspace_keycloak_base_url", "http://kc:8080")
    monkeypatch.setattr(ki.settings, "dataspace_keycloak_realm", "celine")
    monkeypatch.setattr(ki.settings, "dataspace_keycloak_default_password", "")
    monkeypatch.setattr(ki.settings, "dataspace_keycloak_update_existing", True)

    async def _headers():
        return {"Authorization": f"Bearer {SERVICE_TOKEN}"}

    monkeypatch.setattr(ki, "service_auth_headers", _headers)


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


class TestTheCredentialItProvisionsWith:
    """It authenticates as itself, and holds no administrator's password.

    Provisioning used to log in with `grant_type=password` as a realm
    administrator against the master realm: a credential that can do anything to
    any realm, in the environment of the service that serves the public wizard,
    to create users in one realm. It now presents the same service-account token
    every other outbound call uses, whose realm-management roles are
    `manage-users` and `view-users`.
    """

    async def test_every_call_carries_the_service_account_token(
        self, monkeypatch, submission, _enabled
    ):
        seen: list[httpx.Request] = []
        _patch_httpx(monkeypatch, _handler(seen=seen))

        await ki.provision_keycloak_user(submission)

        assert seen, "provisioning should have called the admin API"
        assert all(r.headers.get("Authorization") == f"Bearer {SERVICE_TOKEN}" for r in seen)

    async def test_it_never_logs_in(self, monkeypatch, submission, _enabled):
        """No token endpoint is called with this service's own httpx client: the
        provider mints the token, and there is no password to send."""
        seen: list[httpx.Request] = []
        _patch_httpx(monkeypatch, _handler(seen=seen))

        await ki.provision_keycloak_user(submission)

        assert not [r for r in seen if "openid-connect/token" in r.url.path]


class TestWhatARefusalSays:
    """Keycloak's own response body is written for whoever runs Keycloak, and the
    step error is shown to every REC operator who pressed Approve."""

    async def test_a_refusal_reports_the_status_and_not_the_body(
        self, monkeypatch, submission, _enabled
    ):
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "insufficient scope for realm 'celine'"})

        _patch_httpx(monkeypatch, handle)

        with pytest.raises(ValueError) as exc:
            await ki.provision_keycloak_user(submission)

        assert "403" in str(exc.value)
        assert "insufficient scope" not in str(exc.value)

    async def test_the_body_is_logged(self, monkeypatch, submission, _enabled, caplog):
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "insufficient scope for realm 'celine'"})

        _patch_httpx(monkeypatch, handle)

        with caplog.at_level("WARNING"), pytest.raises(ValueError):
            await ki.provision_keycloak_user(submission)

        assert "insufficient scope" in caplog.text

    async def test_a_missing_address_is_a_configuration_error(
        self, monkeypatch, submission, _enabled
    ):
        """Not a `ValueError`: the admin API turns those into a 422 carrying the
        message, and this one names a setting the operator cannot change."""
        monkeypatch.setattr(ki.settings, "dataspace_keycloak_base_url", "")
        monkeypatch.setattr(ki.settings, "oidc_base_url", "https://issuer.example/oauth2")

        with pytest.raises(ConfigurationError):
            await ki.provision_keycloak_user(submission)


class TestWhichRealmItProvisionsIn:
    """Unset is the normal configuration, and the derived answer is the only one
    that can work: a client-credentials token administers the realm that minted
    it and no other."""

    def test_it_is_the_realm_the_issuer_names(self, monkeypatch):
        monkeypatch.setattr(ki.settings, "dataspace_keycloak_realm", "")
        monkeypatch.setattr(ki.settings, "oidc_base_url", "http://kc.test/realms/celine")

        assert ki.keycloak_realm() == "celine"

    def test_an_explicit_setting_wins(self, monkeypatch):
        monkeypatch.setattr(ki.settings, "dataspace_keycloak_realm", "dataspaces")
        monkeypatch.setattr(ki.settings, "oidc_base_url", "http://kc.test/realms/celine")

        assert ki.keycloak_realm() == "dataspaces"

    def test_an_issuer_naming_no_realm_has_to_be_told(self, monkeypatch):
        monkeypatch.setattr(ki.settings, "dataspace_keycloak_realm", "")
        monkeypatch.setattr(ki.settings, "oidc_base_url", "https://issuer.example/oauth2")

        with pytest.raises(ConfigurationError):
            ki.keycloak_realm()

    async def test_the_derived_realm_is_the_one_called(self, monkeypatch, submission, _enabled):
        monkeypatch.setattr(ki.settings, "dataspace_keycloak_realm", "")
        monkeypatch.setattr(ki.settings, "oidc_base_url", "http://kc.test/realms/derived")
        seen: list[httpx.Request] = []
        _patch_httpx(monkeypatch, _handler(seen=seen))

        await ki.provision_keycloak_user(submission)

        assert all("/admin/realms/derived/" in str(r.url) for r in seen)


class TestWhereTheAdminApiIs:
    """Keycloak checks a token's issuer against the address the request arrived
    on, so the two addresses are not independent. Deriving one from the other
    makes them agree by construction."""

    def test_it_defaults_to_the_issuer_origin(self, monkeypatch):
        monkeypatch.setattr(ki.settings, "dataspace_keycloak_base_url", "")
        monkeypatch.setattr(ki.settings, "oidc_base_url", "http://kc.test:8080/realms/celine")

        assert ki._base_url() == "http://kc.test:8080"

    def test_an_explicit_setting_wins(self, monkeypatch):
        monkeypatch.setattr(ki.settings, "dataspace_keycloak_base_url", "http://internal:8080/")
        monkeypatch.setattr(ki.settings, "oidc_base_url", "http://kc.test:8080/realms/celine")

        assert ki._base_url() == "http://internal:8080"

    def test_a_non_keycloak_issuer_has_to_be_told(self, monkeypatch):
        monkeypatch.setattr(ki.settings, "dataspace_keycloak_base_url", "")
        monkeypatch.setattr(ki.settings, "oidc_base_url", "https://issuer.example/oauth2")

        with pytest.raises(ConfigurationError):
            ki._base_url()


class TestTheTwoRefusalsItEarns:
    """A 401 and a 403 look alike in a step row and are nothing alike: one is a
    credential the realm will not read, the other one it read and found empty."""

    @pytest.fixture()
    def _configured(self, monkeypatch, _enabled):
        monkeypatch.setattr(ki.settings, "oidc_base_url", "http://kc.test/realms/celine")
        monkeypatch.setattr(ki.settings, "ds_onboarding_client_id", "svc-ds-onboarding")

    async def test_a_401_points_at_the_address(self, monkeypatch, submission, _configured, caplog):
        _patch_httpx(monkeypatch, lambda request: httpx.Response(401, text="Unauthorized"))

        with caplog.at_level("WARNING"), pytest.raises(ValueError):
            await ki.provision_keycloak_user(submission)

        assert "not the one that minted it" in caplog.text

    async def test_a_403_points_at_the_roles(self, monkeypatch, submission, _configured, caplog):
        _patch_httpx(monkeypatch, lambda request: httpx.Response(403, text="Forbidden"))

        with caplog.at_level("WARNING"), pytest.raises(ValueError):
            await ki.provision_keycloak_user(submission)

        assert "manage-users" in caplog.text
        assert "svc-ds-onboarding" in caplog.text

    async def test_neither_diagnosis_reaches_the_operator(
        self, monkeypatch, submission, _configured
    ):
        _patch_httpx(monkeypatch, lambda request: httpx.Response(403, text="Forbidden"))

        with pytest.raises(ValueError) as exc:
            await ki.provision_keycloak_user(submission)

        assert str(exc.value) == "Keycloak user lookup failed (403)"
