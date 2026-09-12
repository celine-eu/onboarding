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
    monkeypatch.setattr(ki.settings, "dataspace_keycloak_participants_group", "/participants")
    monkeypatch.setattr(ki.settings, "dataspace_keycloak_update_existing", True)

    async def _headers():
        return {"Authorization": f"Bearer {SERVICE_TOKEN}"}

    monkeypatch.setattr(ki, "keycloak_admin_auth_headers", _headers)


def _patch_httpx(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def factory(**kw):
        kw.pop("transport", None)
        return _OriginalAsyncClient(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


GROUP_ID = "grp-participants"


def _handler(
    *,
    members: list[dict] | None = None,
    outsiders: list[dict] | None = None,
    seen: list[httpx.Request] | None = None,
    group_exists: bool = True,
):
    """A Keycloak that behaves the way the measured one does.

    Four of its behaviours are the reason this module looks as it does, and each
    was observed against 26.6.0 under the group-scoped grant rather than read in
    documentation:

    * the realm-wide user search is **403** — the grant covers one group;
    * `GET /groups/{id}/members` **ignores** `search` and `exact`, answering 200
      with every member however it is queried, so it is paged and matched here;
    * a name already taken is **409**, whether its owner is in the group or not,
      because uniqueness is checked before containment;
    * the group's id comes only from `group-by-path`, which is **404** when the
      realm has no such group.
    """
    members = list(members or [])
    outsiders = list(outsiders or [])

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        method = request.method

        if method == "GET" and "/group-by-path/" in path:
            if not group_exists:
                return httpx.Response(404, json={"error": "Group path does not exist"})
            return httpx.Response(200, json={"id": GROUP_ID, "path": "/participants"})

        if method == "GET" and path.endswith(f"/groups/{GROUP_ID}/members"):
            first = int(request.url.params.get("first", 0))
            page = int(request.url.params.get("max", ki._MEMBER_PAGE))
            # `search` is deliberately ignored, exactly as Keycloak ignores it.
            return httpx.Response(200, json=members[first : first + page])

        if method == "GET" and path.endswith("/users"):
            # The realm-wide search this grant does not reach.
            return httpx.Response(403, json={"error": "HTTP 403 Forbidden"})

        if method == "POST" and path.endswith("/users"):
            body = json.loads(request.content)
            # A creation is refused with 403 — never 404 — both when it names no
            # group and when it names one the realm does not have.
            if body.get("groups") != ["/participants"] or not group_exists:
                return httpx.Response(403, json={"error": "HTTP 403 Forbidden"})
            taken = {
                value.lower()
                for user in members + outsiders
                for value in (user.get("username", ""), user.get("email", ""))
                if value
            }
            if {body.get("username", "").lower(), body.get("email", "").lower()} & taken:
                return httpx.Response(409, json={"errorMessage": "User exists with same username"})
            return httpx.Response(
                201, headers={"Location": "http://kc:8080/admin/realms/celine/users/kc-new"}
            )

        if method == "PUT":
            return httpx.Response(204)
        raise AssertionError(f"unexpected call {method} {request.url}")

    return handle


class TestTheUsernameReported:
    async def test_a_created_user_is_named_by_their_email(self, monkeypatch, submission, _enabled):
        _patch_httpx(monkeypatch, _handler())

        result = await ki.provision_keycloak_user(submission)

        assert result.created is True
        assert result.user_id == "kc-new"
        assert result.username == "user@example.com"

    async def test_an_existing_member_keeps_the_username_keycloak_holds(
        self, monkeypatch, submission, _enabled
    ):
        """Not the email the scan matched on. The scan matches the email too, so
        a user created by anything else is found under a name of its choosing —
        and that is what their token will carry. Reporting the email would hand
        the registry a `user_id` as unresolvable as the one this fixed."""
        _patch_httpx(
            monkeypatch,
            _handler(members=[{"id": "kc-1", "username": "gl-00001", "email": "user@example.com"}]),
        )

        result = await ki.provision_keycloak_user(submission)

        assert result.created is False
        assert result.username == "gl-00001"

    async def test_a_member_with_no_username_falls_back_to_the_email(
        self, monkeypatch, submission, _enabled
    ):
        _patch_httpx(monkeypatch, _handler(members=[{"id": "kc-1", "email": "user@example.com"}]))

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
            _handler(
                members=[{"id": "kc-1", "username": "gl-00001", "email": "user@example.com"}],
                seen=seen,
            ),
        )

        await ki.provision_keycloak_user(submission)

        puts = [r for r in seen if r.method == "PUT"]
        assert puts, "the existing user should still have their profile refreshed"
        assert "username" not in json.loads(puts[0].content)

    async def test_the_update_does_not_claim_to_maintain_membership(
        self, monkeypatch, submission, _enabled
    ):
        """A `PUT` carrying `groups` is accepted with a 204 and silently ignored
        — measured, including a body naming a different group. Sending one would
        read as if this call kept membership up to date, and it does not."""
        seen: list[httpx.Request] = []
        _patch_httpx(
            monkeypatch,
            _handler(
                members=[{"id": "kc-1", "username": "gl-00001", "email": "user@example.com"}],
                seen=seen,
            ),
        )

        await ki.provision_keycloak_user(submission)

        assert "groups" not in json.loads([r for r in seen if r.method == "PUT"][0].content)

    async def test_the_profile_is_still_refreshed(self, monkeypatch, submission, _enabled):
        seen: list[httpx.Request] = []
        _patch_httpx(
            monkeypatch,
            _handler(
                members=[{"id": "kc-1", "username": "gl-00001", "email": "user@example.com"}],
                seen=seen,
            ),
        )

        await ki.provision_keycloak_user(submission)

        body = json.loads([r for r in seen if r.method == "PUT"][0].content)
        assert body["email"] == "user@example.com"
        assert body["firstName"] == "Alice"


class TestTheGroupItCreatesInto:
    """The group is the grant, not a filing convention.

    `celine-policies` grants this service the members of one realm group, so a
    creation that names no group — or names another one — is refused with a 403.
    The `groups` key is what makes the call permitted at all.
    """

    async def test_the_creation_names_the_group(self, monkeypatch, submission, _enabled):
        seen: list[httpx.Request] = []
        _patch_httpx(monkeypatch, _handler(seen=seen))

        await ki.provision_keycloak_user(submission)

        post = [r for r in seen if r.method == "POST"][0]
        assert json.loads(post.content)["groups"] == ["/participants"]

    async def test_a_creation_with_no_group_is_what_keycloak_refuses(
        self, monkeypatch, submission, _enabled
    ):
        """The fake refuses it the way the realm does, so a change that dropped
        the key would fail here rather than in a deployment."""
        monkeypatch.setattr(
            ki, "_user_payload", lambda submission, email, group: {"username": email}
        )
        _patch_httpx(monkeypatch, _handler())

        with pytest.raises(ValueError) as exc:
            await ki.provision_keycloak_user(submission)

        assert "403" in str(exc.value)

    def test_a_path_without_its_leading_slash_is_still_a_path(self, monkeypatch, _enabled):
        monkeypatch.setattr(ki.settings, "dataspace_keycloak_participants_group", "participants")
        assert ki.participants_group() == "/participants"

    def test_a_trailing_slash_is_dropped(self, monkeypatch, _enabled):
        monkeypatch.setattr(ki.settings, "dataspace_keycloak_participants_group", "/participants/")
        assert ki.participants_group() == "/participants"

    def test_no_group_is_a_configuration_error(self, monkeypatch, _enabled):
        """Not a `ValueError`: there is nowhere to put a participant, and that is
        the deployment's to fix rather than the operator's."""
        monkeypatch.setattr(ki.settings, "dataspace_keycloak_participants_group", "  ")
        with pytest.raises(ConfigurationError):
            ki.participants_group()

    async def test_a_realm_without_the_group_is_a_configuration_error(
        self, monkeypatch, submission, _enabled
    ):
        """`group-by-path` is the only call that distinguishes a missing group
        from a missing grant — a creation into a group that does not exist is
        refused with the same 403 as one this service may not touch."""
        _patch_httpx(
            monkeypatch,
            _handler(outsiders=[{"id": "kc-9", "email": "user@example.com"}], group_exists=False),
        )

        with pytest.raises(ConfigurationError) as exc:
            await ki.provision_keycloak_user(submission)

        assert "/participants" in str(exc.value)

    async def test_a_creation_refused_by_a_missing_group_says_so(
        self, monkeypatch, submission, _enabled
    ):
        """The 403 a creation earns is the same whether the grant is wrong or the
        group is absent, so the absent group is looked for before the refusal is
        reported — otherwise a realm that was never synced is told its
        permissions are at fault."""
        _patch_httpx(monkeypatch, _handler(group_exists=False))

        with pytest.raises(ConfigurationError):
            await ki.provision_keycloak_user(submission)

    async def test_a_refusal_that_is_not_a_missing_group_stays_a_refusal(
        self, monkeypatch, submission, _enabled
    ):
        """A grant without the `view` scope cannot resolve the group either. That
        second refusal is not the news — the first one is."""

        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "HTTP 403 Forbidden"})

        _patch_httpx(monkeypatch, handle)

        with pytest.raises(ValueError) as exc:
            await ki.provision_keycloak_user(submission)

        assert str(exc.value) == "Keycloak user creation failed (403)"


class TestFindingSomebodyWhoIsAlreadyThere:
    """There is no search, so this is a paged scan — and it only runs on a 409.

    The realm-wide `GET /users?username=` needs `Users: view` over the whole
    realm, and `GET /groups/{id}/members` answers 200 and ignores `search`
    entirely. Scanning before every creation would therefore page the whole group
    to discover what is almost always a new participant.
    """

    async def test_nothing_is_scanned_when_the_creation_succeeds(
        self, monkeypatch, submission, _enabled
    ):
        seen: list[httpx.Request] = []
        _patch_httpx(monkeypatch, _handler(seen=seen))

        await ki.provision_keycloak_user(submission)

        assert not [r for r in seen if "/members" in r.url.path]

    async def test_the_realm_wide_user_search_is_never_called(
        self, monkeypatch, submission, _enabled
    ):
        seen: list[httpx.Request] = []
        _patch_httpx(
            monkeypatch,
            _handler(
                members=[{"id": "kc-1", "username": "gl-00001", "email": "user@example.com"}],
                seen=seen,
            ),
        )

        await ki.provision_keycloak_user(submission)

        assert not [r for r in seen if r.method == "GET" and r.url.path.endswith("/users")]

    async def test_a_member_past_the_first_page_is_still_found(
        self, monkeypatch, submission, _enabled
    ):
        """The endpoint pages at 100 and ignores every filter, so the participant
        this service is looking for can be anywhere in the group."""
        crowd = [
            {"id": f"kc-{i}", "username": f"gl-{i:05d}", "email": f"p{i}@example.com"}
            for i in range(ki._MEMBER_PAGE + 20)
        ]
        crowd[-1] = {"id": "kc-late", "username": "gl-99999", "email": "user@example.com"}
        seen: list[httpx.Request] = []
        _patch_httpx(monkeypatch, _handler(members=crowd, seen=seen))

        result = await ki.provision_keycloak_user(submission)

        assert result.user_id == "kc-late"
        assert result.username == "gl-99999"
        assert len([r for r in seen if "/members" in r.url.path]) == 2

    async def test_the_scan_matches_the_username_too(self, monkeypatch, submission, _enabled):
        """A user this service created is named by their email, so the two
        columns hold the same value — but one created by `celine-policies` is
        `gl-00001` with an email of its own, and either can be the match."""
        submission.email = "gl-00001"
        _patch_httpx(
            monkeypatch,
            _handler(members=[{"id": "kc-1", "username": "gl-00001", "email": "else@example.com"}]),
        )

        result = await ki.provision_keycloak_user(submission)

        assert result.user_id == "kc-1"
        assert result.created is False


class TestADuplicateOutsideTheGroup:
    """The account exists, and this service cannot see it, adopt it, or move it.

    It is the price of the narrow grant, and it is paid visibly: the step fails
    with what a person has to do, rather than a bare 409 or a silent second
    account.
    """

    @pytest.fixture()
    def _an_outsider(self, monkeypatch):
        _patch_httpx(
            monkeypatch,
            _handler(
                outsiders=[{"id": "kc-out", "username": "someone", "email": "user@example.com"}]
            ),
        )

    async def test_the_step_fails_naming_what_to_do(
        self, monkeypatch, submission, _enabled, _an_outsider
    ):
        with pytest.raises(ValueError) as exc:
            await ki.provision_keycloak_user(submission)

        assert "participants group (/participants)" in str(exc.value)

    async def test_it_says_nothing_about_the_realm_or_the_client(
        self, monkeypatch, submission, _enabled, _an_outsider
    ):
        """A REC operator reads this. The realm, the client and the grant are the
        platform's business and go to the log."""
        with pytest.raises(ValueError) as exc:
            await ki.provision_keycloak_user(submission)

        assert "celine" not in str(exc.value)
        assert "svc-onboarding" not in str(exc.value)

    async def test_the_detail_is_logged(
        self, monkeypatch, submission, _enabled, _an_outsider, caplog
    ):
        with caplog.at_level("WARNING"), pytest.raises(ValueError):
            await ki.provision_keycloak_user(submission)

        assert "user@example.com" in caplog.text
        assert "/participants" in caplog.text

    async def test_no_second_account_is_left_behind(self, monkeypatch, submission, _enabled):
        """The 409 is Keycloak refusing to create one, and nothing here retries
        under another name."""
        seen: list[httpx.Request] = []
        _patch_httpx(
            monkeypatch,
            _handler(
                outsiders=[{"id": "kc-out", "username": "someone", "email": "user@example.com"}],
                seen=seen,
            ),
        )

        with pytest.raises(ValueError):
            await ki.provision_keycloak_user(submission)

        assert len([r for r in seen if r.method == "POST"]) == 1


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
    celine's own client presents everywhere else, whose realm-management roles
    are `manage-users` and `view-users`.
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
        monkeypatch.setattr(ki.settings, "oidc_client_id", "svc-onboarding")

    async def test_a_401_points_at_the_address(self, monkeypatch, submission, _configured, caplog):
        _patch_httpx(monkeypatch, lambda request: httpx.Response(401, text="Unauthorized"))

        with caplog.at_level("WARNING"), pytest.raises(ValueError):
            await ki.provision_keycloak_user(submission)

        assert "not the one that minted it" in caplog.text

    async def test_a_403_points_at_the_roles(self, monkeypatch, submission, _configured, caplog):
        _patch_httpx(monkeypatch, lambda request: httpx.Response(403, text="Forbidden"))

        with caplog.at_level("WARNING"), pytest.raises(ValueError):
            await ki.provision_keycloak_user(submission)

        assert "manage-members" in caplog.text
        assert "svc-onboarding" in caplog.text
        assert "/participants" in caplog.text
        # A missing group is refused with the same 403, so the operator is told
        # to check for it rather than left to conclude the grant is wrong.
        assert "synced" in caplog.text

    async def test_neither_diagnosis_reaches_the_operator(
        self, monkeypatch, submission, _configured
    ):
        _patch_httpx(monkeypatch, lambda request: httpx.Response(403, text="Forbidden"))

        with pytest.raises(ValueError) as exc:
            await ki.provision_keycloak_user(submission)

        assert str(exc.value) == "Keycloak user creation failed (403)"


class TestWhichIdentityAdministersTheRealm:
    """celine's own client, not the dataspace's.

    They are granted by different people for different things: the dataspace's
    client carries what a dataspace deployment gives this service, and
    administering users in celine's realm is not one of those. The two are kept
    apart so that neither grant can be reached with the other's secret.
    """

    @pytest.fixture()
    def _both_clients(self, monkeypatch):
        from celine.onboarding.services import service_auth

        service_auth.reset_token_providers()
        monkeypatch.setattr(service_auth.settings, "oidc_base_url", "http://kc.test/realms/celine")
        monkeypatch.setattr(service_auth.settings, "oidc_client_id", "svc-onboarding")
        monkeypatch.setattr(service_auth.settings, "oidc_client_secret", "onboarding-secret")
        monkeypatch.setattr(service_auth.settings, "ds_onboarding_client_id", "svc-ds-onboarding")
        monkeypatch.setattr(service_auth.settings, "ds_onboarding_client_secret", "ds-secret")
        yield service_auth
        service_auth.reset_token_providers()

    def test_provisioning_uses_celines_client(self, _both_clients):
        # `_client_id` is the SDK provider's own attribute; there is no public
        # reader for it, and which client is being presented is the whole point.
        assert _both_clients.keycloak_admin_token_provider()._client_id == "svc-onboarding"

    def test_the_dataspace_keeps_its_own(self, _both_clients):
        assert _both_clients.service_token_provider()._client_id == "svc-ds-onboarding"

    def test_they_are_separate_credentials(self, _both_clients):
        assert (
            _both_clients.keycloak_admin_token_provider()
            is not _both_clients.service_token_provider()
        )

    def test_an_unconfigured_issuer_is_a_configuration_error(self, monkeypatch, _both_clients):
        monkeypatch.setattr(_both_clients.settings, "oidc_base_url", "")
        _both_clients.reset_token_providers()

        with pytest.raises(ConfigurationError):
            _both_clients.keycloak_admin_token_provider()
