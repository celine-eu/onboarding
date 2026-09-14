"""Provisioning the login, and what the rest of enablement is told about it.

The login is the first effect of approval, and the *username* the account is
created or found under is the value the REC registry needs: every self-service
route there resolves a caller by matching `Member.user_id` against the token's
`preferred_username`. So what this module reports is not a detail — it decides
whether an approved participant can ever see their own membership (issue #1).

**This exercises the wire, not a fake client.** The predecessor of this file
mocked `httpx` and asserted against a Keycloak that behaved the way the measured
one did; there is no Keycloak here any more, and what replaced it is a contract
with one service. `respx` in front of the real `celine.sdk.provisioning` client
is what makes the assertions about that contract — the route shape, the body,
and which of its status codes means what — rather than about a stub somebody
wrote to agree with the code.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from celine.onboarding.services import provisioning as pv
from celine.onboarding.services.errors import ConfigurationError

PROVISIONING = "http://provisioning.test"
COMMUNITY = "greenland"


def _sub(**overrides):
    base = dict(
        ref="20260727-abcd",
        rec_slug="example",
        email="Alice.Rossi@example.org",
        first_name="Alice",
        last_name="Rossi",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def bound(bind_rec):
    """A REC whose manifest declares the registry community to key accounts on."""
    manifest = bind_rec("example")
    manifest["rec_registry"] = {"community": COMMUNITY, "default_area": "north"}
    return manifest


@pytest.fixture()
def enabled(monkeypatch):
    """`PROVISIONING_URL` set, and a client that presents a static token.

    The client is built here rather than through `_get_client` so the test does
    not need an OIDC provider to mint anything — which client is presented is a
    separate question, asked in `TestWhichIdentityAsksForTheLogin`.
    """
    from celine.sdk.provisioning import ProvisioningClient

    monkeypatch.setattr(pv.settings, "provisioning_url", PROVISIONING)
    pv.reset_client()
    monkeypatch.setattr(
        pv,
        "_client",
        ProvisioningClient(base_url=PROVISIONING, default_token="service-account-token"),
    )
    yield
    pv.reset_client()


@pytest.fixture()
def api():
    with respx.mock(base_url=PROVISIONING, assert_all_called=False) as mock:
        yield mock


def upsert_route(api, *, community=COMMUNITY, key="20260727-abcd", **body):
    payload = {
        "user_id": "kc-uuid-1",
        "username": "alice.rossi@example.org",
        "created": True,
        "invitation": "sent",
        "invited": True,
    }
    payload.update(body)
    return api.put(f"/participants/{community}/{key}").mock(
        return_value=httpx.Response(200, json=payload)
    )


def error(status: int, code: str, message: str) -> httpx.Response:
    """The service's error body since its 1.2.0 contract: `detail` is `{code, message}`."""
    return httpx.Response(status, json={"detail": {"code": code, "message": message}})


def disable_route(api, *, community=COMMUNITY, key="20260727-abcd", **body):
    payload = {"user_id": "kc-uuid-1", "username": "alice.rossi@example.org", "changed": True}
    payload.update(body)
    return api.post(f"/participants/{community}/{key}/disable").mock(
        return_value=httpx.Response(200, json=payload)
    )


# ── whether a login is provisioned at all ─────────────────────────────────────


class TestWhetherThereIsAProvisioningService:
    """An address is the gate, not a flag beside it.

    The same shape `REC_REGISTRY_URL` and `DS_CONNECTOR_URL` already have here.
    A flag saying logins are provisioned while no address is configured is not a
    configuration, it is a contradiction — and it is the one the removed
    `DATASPACE_KEYCLOAK_ENABLED` could express.
    """

    def test_no_url_means_no_login(self, monkeypatch):
        monkeypatch.setattr(pv.settings, "provisioning_url", "")
        assert pv.provisioning_enabled() is False

    def test_whitespace_is_not_an_address(self, monkeypatch):
        monkeypatch.setattr(pv.settings, "provisioning_url", "   ")
        assert pv.provisioning_enabled() is False

    def test_an_url_enables_it(self, monkeypatch):
        monkeypatch.setattr(pv.settings, "provisioning_url", PROVISIONING)
        assert pv.provisioning_enabled() is True

    def test_reading_the_url_without_one_is_a_configuration_error(self, monkeypatch):
        monkeypatch.setattr(pv.settings, "provisioning_url", "")
        with pytest.raises(ConfigurationError) as exc:
            pv.provisioning_url()
        assert "PROVISIONING_URL" in str(exc.value)

    def test_a_trailing_slash_is_dropped(self, monkeypatch):
        monkeypatch.setattr(pv.settings, "provisioning_url", "http://provisioning:8010/")
        assert pv.provisioning_url() == "http://provisioning:8010"

    async def test_a_disabled_deployment_provisions_nothing(self, monkeypatch, bound):
        monkeypatch.setattr(pv.settings, "provisioning_url", "")
        assert await pv.provision_participant(_sub()) is None


# ── the community the account is keyed on ─────────────────────────────────────


class TestTheCommunity:
    """`rec_registry.community`, and no second answer to which community.

    The provisioning service files the account into that community's Keycloak
    organization, and the sweep that later checks the filing reads the
    community's own id from the registry export. A different alias here would
    put a participant in an organization nothing looks at.
    """

    async def test_it_is_the_registry_binding(self, bound):
        assert await pv.participant_community("example") == COMMUNITY

    async def test_no_registry_block_means_no_community(self, bind_rec):
        bind_rec("plain")
        assert await pv.participant_community("plain") is None

    async def test_a_rec_with_no_binding_gets_no_login(self, monkeypatch, bind_rec, enabled):
        """The narrowing this change makes, asserted rather than implied.

        Revocation resolves `(community, key)` through the registry export, so a
        login provisioned for a REC with no registry could never be revoked
        through this seam. A missing login is the better of the two.
        """
        bind_rec("plain")
        assert await pv.provision_participant(_sub(rec_slug="plain")) is None


class TestWhatTheWizardIsTold:
    """Whether the wizard may promise an email to set a password.

    Approval sends the invitation only where a login is provisioned at all, so
    the promise follows exactly the two conditions step 1 checks. A person told
    to expect an email that never comes has been told something false.
    """

    async def test_a_bound_rec_on_a_provisioning_deployment_gets_one(self, bound, enabled):
        assert await pv.login_is_provisioned("example") is True

    async def test_no_provisioning_service_means_no_promise(self, monkeypatch, bound):
        monkeypatch.setattr(pv.settings, "provisioning_url", "")
        assert await pv.login_is_provisioned("example") is False

    async def test_no_registry_binding_means_no_promise(self, bind_rec, enabled):
        bind_rec("plain")
        assert await pv.login_is_provisioned("plain") is False

    def test_the_public_config_carries_it(self, bound, enabled, bind_rec):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from celine.onboarding.api.config import router

        bind_rec("plain")
        app = FastAPI()
        app.include_router(router, prefix="/api/{rec_slug}")
        client = TestClient(app)

        assert client.get("/api/example/config").json()["login_invitation"] is True
        assert client.get("/api/plain/config").json()["login_invitation"] is False


# ── the upsert ────────────────────────────────────────────────────────────────


class TestTheUpsertAsksForAnInvitation:
    """Approval always asks; the provisioning service decides whether one is sent.

    This repository cannot see credentials, so it does not decide whether an
    account "needs" an invitation. The service sends only to an account created
    in the call or one without a password, which is what makes a retry safe.
    """

    async def test_invite_is_true(self, bound, enabled, api):
        route = upsert_route(api)
        await pv.provision_participant(_sub())
        assert json.loads(route.calls[0].request.content)["invite"] is True

    async def test_a_retry_asks_again(self, bound, enabled, api):
        route = upsert_route(api, created=False, invitation="has_password", invited=False)
        await pv.provision_participant(_sub())
        await pv.provision_participant(_sub())
        assert [json.loads(c.request.content)["invite"] for c in route.calls] == [True, True]

    @pytest.mark.parametrize(
        ("code", "invited"),
        [
            ("sent", True),
            ("has_password", False),
            ("not_on_dev_list", False),
            ("account_disabled", False),
            ("not_requested", False),
            ("cooldown", False),
            ("send_failed", False),
            ("no_email", False),
        ],
    )
    async def test_the_outcome_comes_back_as_a_plain_code(self, bound, enabled, api, code, invited):
        """The generated schema holds an `Enum`; the step row and the console need
        the string, and `invited` is derived from it here rather than trusted."""
        upsert_route(api, created=False, invitation=code, invited=invited)
        result = await pv.provision_participant(_sub())
        assert result.invitation == code
        assert isinstance(result.invitation, str)
        assert result.invited is invited

    @pytest.mark.parametrize("code", ["cooldown", "send_failed", "no_email"])
    async def test_an_email_that_did_not_go_out_is_not_a_failure(self, bound, enabled, api, code):
        """`200` with the code: the account exists, so step 1 succeeds and says so."""
        upsert_route(api, created=True, invitation=code, invited=False)
        result = await pv.provision_participant(_sub())
        assert result.user_id == "kc-uuid-1"
        assert result.invitation == code
        assert result.invited is False

    async def test_a_disabled_account_is_not_a_failure(self, bound, enabled, api):
        """Re-approval after revocation: `200` with `account_disabled` (O3). The
        account exists, so the step succeeds and says the email did not go out."""
        upsert_route(api, created=False, invitation="account_disabled", invited=False)
        result = await pv.provision_participant(_sub())
        assert result.user_id == "kc-uuid-1"
        assert result.invitation == "account_disabled"


class TestTheInvitationLanguage:
    """Submission, then manifest, then nothing — each narrowed to `it|en|es`.

    The service answers `422` for anything else, and step 1 fails closed, so a
    value it would refuse is dropped rather than sent: the realm default gives
    the same email without blocking an approval.
    """

    @staticmethod
    def _sent_locale(route):
        return json.loads(route.calls[0].request.content).get("locale")

    async def test_the_submissions_own_language_wins(self, bound, enabled, api):
        bound["locale"] = "it"
        route = upsert_route(api)
        await pv.provision_participant(_sub(locale="es"))
        assert self._sent_locale(route) == "es"

    async def test_the_manifest_language_is_the_fallback(self, bound, enabled, api):
        bound["locale"] = "en"
        route = upsert_route(api)
        await pv.provision_participant(_sub(locale=None))
        assert self._sent_locale(route) == "en"

    async def test_with_neither_no_locale_is_sent(self, bound, enabled, api):
        route = upsert_route(api)
        await pv.provision_participant(_sub(locale=None))
        assert "locale" not in json.loads(route.calls[0].request.content)

    @pytest.mark.parametrize("manifest_locale", ["it-IT", "fr", "IT", ""])
    async def test_a_manifest_value_the_service_would_refuse_sends_nothing(
        self, bound, enabled, api, manifest_locale
    ):
        bound["locale"] = manifest_locale
        route = upsert_route(api)
        result = await pv.provision_participant(_sub(locale=None))
        assert route.called and result is not None
        assert "locale" not in json.loads(route.calls[0].request.content)

    async def test_an_unsupported_submission_value_falls_through_to_the_manifest(
        self, bound, enabled, api
    ):
        """Unreachable through the wizard, which refuses it at capture; a row
        written some other way must still not block approval."""
        bound["locale"] = "it"
        route = upsert_route(api)
        await pv.provision_participant(_sub(locale="fr"))
        assert self._sent_locale(route) == "it"

    async def test_a_retry_sends_the_language_again(self, bound, enabled, api):
        route = upsert_route(api, created=False)
        await pv.provision_participant(_sub(locale="es"))
        await pv.provision_participant(_sub(locale="es"))
        assert [json.loads(c.request.content)["locale"] for c in route.calls] == ["es", "es"]

    def test_the_consent_locale_is_not_read(self, bind_rec):
        """It is evidence of what a consent text was shown in, not a preference."""
        bind_rec("example")
        sub = _sub(locale=None, data_sharing_consent_locale="es")
        assert pv.participant_locale(sub) is None


class TestProvisioningAParticipant:
    async def test_it_puts_the_community_and_the_member_key(self, bound, enabled, api):
        route = upsert_route(api)
        await pv.provision_participant(_sub())
        assert route.called
        assert route.calls[0].request.url.path == f"/participants/{COMMUNITY}/20260727-abcd"

    async def test_the_address_is_normalised(self, bound, enabled, api):
        route = upsert_route(api)
        await pv.provision_participant(_sub(email="  Alice.Rossi@Example.ORG "))
        assert json.loads(route.calls[0].request.content)["email"] == "alice.rossi@example.org"

    async def test_the_names_travel(self, bound, enabled, api):
        route = upsert_route(api)
        await pv.provision_participant(_sub())
        body = json.loads(route.calls[0].request.content)
        assert body["first_name"] == "Alice"
        assert body["last_name"] == "Rossi"

    async def test_blank_names_are_omitted_rather_than_sent_empty(self, bound, enabled, api):
        route = upsert_route(api)
        await pv.provision_participant(_sub(first_name="  ", last_name=None))
        body = json.loads(route.calls[0].request.content)
        assert body.get("first_name") is None
        assert body.get("last_name") is None

    async def test_the_token_is_presented(self, bound, enabled, api):
        route = upsert_route(api)
        await pv.provision_participant(_sub())
        assert route.calls[0].request.headers["Authorization"] == "Bearer service-account-token"

    async def test_the_username_comes_back_from_the_service(self, bound, enabled, api):
        """Read back, never computed.

        An account that already existed may authenticate under a convention
        nobody here chose — a participant seeded from a registry file, or one
        made by hand. Computing the name from the address would be right for
        every account the platform created and silently wrong for every account
        it adopted, and the value is what reaches `Member.user_id`.
        """
        upsert_route(api, username="gl-00001", created=False)
        result = await pv.provision_participant(_sub())
        assert result.username == "gl-00001"
        assert result.created is False

    async def test_the_user_id_is_the_keycloak_uuid(self, bound, enabled, api):
        upsert_route(api, user_id="3f1c-uuid")
        result = await pv.provision_participant(_sub())
        assert result.user_id == "3f1c-uuid"

    async def test_a_submission_with_no_address_cannot_be_provisioned(self, bound, enabled, api):
        upsert_route(api)
        with pytest.raises(ValueError) as exc:
            await pv.provision_participant(_sub(email=""))
        assert "no email" in str(exc.value)


class TestWhenProvisioningRefuses:
    """Two audiences, and one message would serve neither.

    A missing scope or credential is a platform operator's to fix and reads as
    an unactionable sentence on a REC operator's screen, so it is raised as
    `ConfigurationError` — which the enablement runner already keeps out of the
    step row and puts in the log. An outage is not.
    """

    async def test_no_scope_is_a_configuration_error(self, bound, enabled, api):
        api.put(f"/participants/{COMMUNITY}/20260727-abcd").mock(
            return_value=error(403, "insufficient_scope", "requires scope '…'")
        )
        with pytest.raises(ConfigurationError) as exc:
            await pv.provision_participant(_sub())
        assert "provisioning.participants.write" in str(exc.value)

    async def test_a_bad_credential_is_a_configuration_error(self, bound, enabled, api):
        api.put(f"/participants/{COMMUNITY}/20260727-abcd").mock(
            return_value=error(401, "invalid_token", "invalid token")
        )
        with pytest.raises(ConfigurationError) as exc:
            await pv.provision_participant(_sub())
        assert "OIDC_CLIENT_SECRET" in str(exc.value)

    async def test_a_dependency_failure_is_an_ordinary_failure(self, bound, enabled, api):
        """`502` is Keycloak having failed, not a refusal — and it is the one
        status here worth retrying, which is what the step row is for."""
        api.put(f"/participants/{COMMUNITY}/20260727-abcd").mock(
            return_value=error(502, "provisioning_failed", "keycloak unreachable")
        )
        with pytest.raises(ValueError) as exc:
            await pv.provision_participant(_sub())
        assert not isinstance(exc.value, ConfigurationError)
        assert "502" in str(exc.value)

    @pytest.mark.parametrize(
        ("code", "says"),
        [
            ("registry_unavailable", "could not reach the REC registry"),
            ("provisioning_failed", "Keycloak failed behind the provisioning service"),
            ("send_failed", "Keycloak could not send the email"),
        ],
    )
    async def test_each_dependency_failure_says_which_and_to_retry(
        self, bound, enabled, api, code, says
    ):
        api.put(f"/participants/{COMMUNITY}/20260727-abcd").mock(
            return_value=error(502, code, "realm celine: connection refused")
        )
        with pytest.raises(ValueError) as exc:
            await pv.provision_participant(_sub())
        assert not isinstance(exc.value, ConfigurationError)
        assert says in str(exc.value)
        assert code in str(exc.value)
        assert "retry" in str(exc.value)
        assert "connection refused" not in str(exc.value)

    async def test_an_unknown_community_is_a_configuration_error(self, bound, enabled, api):
        """The manifest binds a community the registry does not hold: no retry fixes it."""
        api.put(f"/participants/{COMMUNITY}/20260727-abcd").mock(
            return_value=error(404, "community_not_found", "no community 'greenland'")
        )
        with pytest.raises(ConfigurationError) as exc:
            await pv.provision_participant(_sub())
        assert COMMUNITY in str(exc.value)
        assert "rec_registry.community" in str(exc.value)

    async def test_an_unknown_code_is_shown_beside_the_status(self, bound, enabled, api):
        api.put(f"/participants/{COMMUNITY}/20260727-abcd").mock(
            return_value=error(502, "something_new", "…")
        )
        with pytest.raises(ValueError, match=r"\(502 something_new\)"):
            await pv.provision_participant(_sub())

    async def test_the_refusal_body_is_not_shown_to_the_operator(self, bound, enabled, api, caplog):
        """It is written for whoever runs the provisioning service.

        The step error reaches every REC operator with a review queue, so what
        the body says about the realm, the client or the request goes to the log
        and the status is what somebody can act on.
        """
        api.put(f"/participants/{COMMUNITY}/20260727-abcd").mock(
            return_value=error(502, "provisioning_failed", "realm celine: connection refused")
        )
        with caplog.at_level("WARNING"):
            with pytest.raises(ValueError) as exc:
                await pv.provision_participant(_sub())
        assert "connection refused" not in str(exc.value)
        assert "connection refused" in caplog.text


# ── revocation ────────────────────────────────────────────────────────────────


class TestRevokingALogin:
    async def test_it_posts_the_disable_route(self, bound, enabled, api):
        route = disable_route(api)
        await pv.disable_participant(_sub())
        assert route.called
        assert route.calls[0].request.url.path == f"/participants/{COMMUNITY}/20260727-abcd/disable"

    async def test_a_revocation_that_happened_says_so(self, bound, enabled, api):
        disable_route(api, changed=True, username="alice.rossi@example.org")
        assert "disabled login alice.rossi@example.org" == await pv.disable_participant(_sub())

    async def test_one_already_in_force_is_not_reported_as_new(self, bound, enabled, api):
        disable_route(api, changed=False, username="alice.rossi@example.org")
        detail = await pv.disable_participant(_sub())
        assert "already disabled" in detail

    @pytest.mark.parametrize(
        ("code", "detail"),
        [
            ("account_not_found", "no account to disable"),
            ("member_not_found", "no member to disable"),
        ],
    )
    async def test_no_member_or_no_account_counts_as_done(self, bound, enabled, api, code, detail):
        """The same reading `deactivate_member` gives a 404.

        The service resolved the lookup and there is nothing left to revoke, and
        refusing would leave the local record claiming something that is no
        longer true.
        """
        api.post(f"/participants/{COMMUNITY}/20260727-abcd/disable").mock(
            return_value=error(404, code, "greenland has no member '…'")
        )
        assert await pv.disable_participant(_sub()) == detail

    async def test_an_unknown_community_fails_the_revocation(self, bound, enabled, api):
        """`community_not_found` is not "nothing to revoke".

        The service could not look the member up, so it cannot say whether a login
        exists. Reporting success would mark the row revoked while the person can
        still sign in.
        """
        api.post(f"/participants/{COMMUNITY}/20260727-abcd/disable").mock(
            return_value=error(404, "community_not_found", "no community 'greenland'")
        )
        with pytest.raises(ConfigurationError) as exc:
            await pv.disable_participant(_sub())
        assert "revoking a login" in str(exc.value)

    @pytest.mark.parametrize(
        "response",
        [
            # FastAPI's own unrouted 404, which a wrong PROVISIONING_URL produces.
            httpx.Response(404, json={"detail": "Not Found"}),
            httpx.Response(404, json={"detail": {"code": "something_new", "message": "…"}}),
            httpx.Response(404, text="not json"),
        ],
    )
    async def test_a_404_without_a_known_code_fails_the_revocation(
        self, bound, enabled, api, response
    ):
        api.post(f"/participants/{COMMUNITY}/20260727-abcd/disable").mock(return_value=response)
        with pytest.raises(ValueError, match="revoking a login"):
            await pv.disable_participant(_sub())

    async def test_a_revocation_that_cannot_find_the_community_leaves_the_row_unrevoked(
        self, bound, enabled, api
    ):
        """Through the runner: the login row is `failed`, not back to `pending`."""
        from test_enablement import FakeDb, FakeSubmission

        from celine.onboarding.models.enablement import EnablementStatus, EnablementStep
        from celine.onboarding.services import enablement

        api.post(f"/participants/{COMMUNITY}/20260727-abcd/disable").mock(
            return_value=error(404, "community_not_found", "no community 'greenland'")
        )
        db = FakeDb()
        submission = FakeSubmission(ref="20260727-abcd", rec_slug="example")
        rows = await enablement.ensure_rows(db, submission)
        login = rows[EnablementStep.KEYCLOAK_USER]
        login.status = EnablementStatus.SUCCEEDED
        login.external_ref = "kc-uuid-1"
        login.invitation = "sent"

        await enablement.revoke(db, submission)

        assert login.status == EnablementStatus.FAILED
        assert login.external_ref == "kc-uuid-1"
        assert "revoke failed" in login.last_error

    async def test_a_refusal_still_raises(self, bound, enabled, api):
        api.post(f"/participants/{COMMUNITY}/20260727-abcd/disable").mock(
            return_value=error(403, "insufficient_scope", "requires scope '…'")
        )
        with pytest.raises(ConfigurationError):
            await pv.disable_participant(_sub())

    async def test_nothing_to_revoke_without_a_service(self, monkeypatch, bound):
        monkeypatch.setattr(pv.settings, "provisioning_url", "")
        assert await pv.disable_participant(_sub()) == "no provisioning service is configured"

    async def test_nothing_to_revoke_without_a_binding(self, bind_rec, enabled):
        bind_rec("plain")
        detail = await pv.disable_participant(_sub(rec_slug="plain"))
        assert "no rec_registry binding" in detail


# ── the fallback username, and the realm ──────────────────────────────────────


class TestTheFallbackUsername:
    """A proxy for a step running without provisioning in the same pass.

    A retry of registry registration alone has no returned value to use, which
    is why `member_user_id` falls back to this rather than requiring one.
    """

    def test_it_is_the_normalised_address(self):
        assert pv.participant_username(_sub()) == "alice.rossi@example.org"

    def test_no_address_means_no_proxy(self):
        assert pv.participant_username(_sub(email="")) is None


class TestTheRealmTheAccountLivesIn:
    """Reported, not administered.

    The dataspace step tells the identity registry where to find the account,
    and the answer has to match where the provisioning service put it.
    """

    def test_it_defaults_to_the_realm_the_issuer_names(self, monkeypatch):
        monkeypatch.setattr(pv.settings, "dataspace_keycloak_realm", "")
        monkeypatch.setattr(pv.settings, "oidc_base_url", "http://kc.test/realms/celine")
        assert pv.keycloak_realm() == "celine"

    def test_an_explicit_realm_wins(self, monkeypatch):
        monkeypatch.setattr(pv.settings, "dataspace_keycloak_realm", "dataspaces")
        monkeypatch.setattr(pv.settings, "oidc_base_url", "http://kc.test/realms/celine")
        assert pv.keycloak_realm() == "dataspaces"

    def test_an_issuer_naming_no_realm_needs_one_stated(self, monkeypatch):
        monkeypatch.setattr(pv.settings, "dataspace_keycloak_realm", "")
        monkeypatch.setattr(pv.settings, "oidc_base_url", "https://auth.example.com/")
        with pytest.raises(ConfigurationError) as exc:
            pv.keycloak_realm()
        assert "DATASPACE_KEYCLOAK_REALM" in str(exc.value)


class TestWhichIdentityAsksForTheLogin:
    """celine's own client, not the dataspace's.

    They are granted by different people for different things: the dataspace's
    client carries what a dataspace deployment gives this service, and asking
    for a login in celine's realm is not one of those. The two are kept apart so
    that neither grant can be reached with the other's secret.

    Note what this client no longer is. It used to administer the realm under a
    fine-grained grant over one group, which is why the reader it is fetched
    with was called `keycloak_admin_token_provider`. It holds no Keycloak right
    now — only the scope `provisioning.participants.write`.
    """

    @pytest.fixture()
    def both_clients(self, monkeypatch):
        from celine.onboarding.services import service_auth

        service_auth.reset_token_providers()
        monkeypatch.setattr(service_auth.settings, "oidc_base_url", "http://kc.test/realms/celine")
        monkeypatch.setattr(service_auth.settings, "oidc_client_id", "svc-onboarding")
        monkeypatch.setattr(service_auth.settings, "oidc_client_secret", "onboarding-secret")
        monkeypatch.setattr(service_auth.settings, "ds_onboarding_client_id", "svc-ds-onboarding")
        monkeypatch.setattr(service_auth.settings, "ds_onboarding_client_secret", "ds-secret")
        yield service_auth
        service_auth.reset_token_providers()

    def test_provisioning_uses_celines_client(self, both_clients):
        # `_client_id` is the SDK provider's own attribute; there is no public
        # reader for it, and which client is being presented is the whole point.
        assert both_clients.celine_token_provider()._client_id == "svc-onboarding"

    def test_the_dataspace_keeps_its_own(self, both_clients):
        assert both_clients.service_token_provider()._client_id == "svc-ds-onboarding"

    def test_they_are_separate_credentials(self, both_clients):
        assert both_clients.celine_token_provider() is not both_clients.service_token_provider()

    def test_an_unconfigured_issuer_is_a_configuration_error(self, monkeypatch, both_clients):
        monkeypatch.setattr(both_clients.settings, "oidc_base_url", "")
        both_clients.reset_token_providers()

        with pytest.raises(ConfigurationError):
            both_clients.celine_token_provider()


class TestNothingHereAdministersKeycloak:
    """The property the whole change exists to establish, asserted once.

    A fallback path to the Admin API is a grant somebody has to keep granting,
    so there is none — and this is the test that fails if one comes back.
    """

    def test_the_admin_api_module_is_gone(self):
        with pytest.raises(ModuleNotFoundError):
            import celine.onboarding.services.keycloak_identity  # noqa: F401

    def test_no_source_file_reaches_the_admin_api(self):
        import pathlib

        root = pathlib.Path(pv.__file__).resolve().parent.parent
        offenders = [
            path.relative_to(root)
            for path in root.rglob("*.py")
            if "/admin/realms/" in path.read_text()
        ]
        assert offenders == []

    def test_the_removed_settings_are_not_readable(self):
        for name in (
            "dataspace_keycloak_enabled",
            "dataspace_keycloak_base_url",
            "dataspace_keycloak_participants_group",
            "dataspace_keycloak_update_existing",
        ):
            assert not hasattr(pv.settings, name), name
