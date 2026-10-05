"""Acting for a community as its collector client (ds collector contract v1).

What is asserted here:

- the alias -> secret map (`SVC_DS_COLLECTOR_<ALIAS>_SECRET`), from the environment
  and from a dotenv file, and the client id it pairs with;
- one scope per token, cached per (alias, scope), and the token request itself
  asking for exactly that scope;
- which scope each call site asks for;
- the development transition (dev falls back to the pre-collector clients, with a
  warning) against the refusal anywhere else, at boot and at the call;
- a refused membership delete as a failure, without hiding the Keycloak failure
  that started a rollback.

The member's own token on ds's person routes is in `test_member_token_forwarding.py`.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from celine.sdk.posture import InsecureConfiguration

import celine.onboarding.services.dataspace_identity as di
from celine.onboarding.config.settings import Settings
from celine.onboarding.services import service_auth
from celine.onboarding.services.errors import ConfigurationError

_OriginalAsyncClient = httpx.AsyncClient

ALIAS = "example-rec"
ENV_NAME = "SVC_DS_COLLECTOR_EXAMPLE_REC_SECRET"
CLIENT_ID = "svc-ds-collector-example-rec"

CREDENTIAL_RESPONSE = {
    "subjectDid": "did:web:users.example:subject-1",
    "credentialId": "urn:uuid:cred-001",
    "generatedAt": "2026-10-05T10:00:00Z",
}


def _patch_httpx(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def factory(**kw):
        kw.pop("transport", None)
        return _OriginalAsyncClient(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _named_token_provider(name: str):
    token = MagicMock()
    token.access_token = name
    token.is_valid.return_value = True
    provider = AsyncMock()
    provider.get_token.return_value = token
    return provider


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    service_auth.reset_token_providers()
    di._token_provider = None
    monkeypatch.delenv(ENV_NAME, raising=False)
    monkeypatch.setattr(service_auth.settings, "ds_collector_secrets", {})
    monkeypatch.setattr(service_auth.settings, "oidc_base_url", "http://kc:8080/realms/test")
    yield
    service_auth.reset_token_providers()
    di._token_provider = None


@pytest.fixture()
def tokens(monkeypatch):
    """Every collector token names its client and its scope; this service's is `service`.

    Only the token endpoint is stubbed: `collector_token_provider` still derives
    the client id and still picks the secret, which is what is being tested.
    """
    built: list[tuple[str, str, str | None]] = []

    def _provider(client_id, client_secret, *, scope=None):
        built.append((client_id, client_secret, scope))
        return _named_token_provider(f"{client_id}|{scope}")

    monkeypatch.setattr(service_auth, "_provider_for", _provider)
    di._token_provider = _named_token_provider("service")
    return built


@pytest.fixture()
def bound(monkeypatch, bind_rec):
    bind_rec(
        "default",
        organization=ALIAS,
        organization_did="did:web:rec.example",
        linked_participant_did="did:web:rec.example",
    )
    monkeypatch.setattr(di.settings, "dataspace_enabled", True)
    monkeypatch.setattr(di.settings, "identity_registry_url", "http://ir:30005")
    monkeypatch.setattr(di.settings, "ds_connector_url", "http://connector:30001")


def _bearer(scope: str) -> str:
    return f"Bearer {CLIENT_ID}|{scope}"


# ── the alias -> secret map ─────────────────────────────────────────


class TestTheSecretMap:
    """
    @verifies REQ-0035
    """

    @pytest.mark.parametrize(
        ("alias", "name"),
        [
            ("example-rec", "SVC_DS_COLLECTOR_EXAMPLE_REC_SECRET"),
            ("rec", "SVC_DS_COLLECTOR_REC_SECRET"),
            ("example-rec-2", "SVC_DS_COLLECTOR_EXAMPLE_REC_2_SECRET"),
        ],
    )
    def test_the_variable_is_the_alias_upper_cased_with_underscores(self, alias, name):
        """
        @verifies REQ-0035
        """
        assert service_auth.collector_secret_env(alias) == name

    def test_the_client_is_the_communitys_collector(self):
        """
        @verifies REQ-0035
        """
        assert service_auth.collector_client_id(ALIAS) == CLIENT_ID
        with pytest.raises(ConfigurationError):
            service_auth.collector_client_id(" ")

    def test_each_community_reads_its_own_secret(self, monkeypatch):
        """Two communities in one instance, two secrets: neither reads the other's.

        @verifies REQ-0035
        """
        monkeypatch.setenv(ENV_NAME, "secret-a")
        monkeypatch.setenv("SVC_DS_COLLECTOR_OTHER_REC_SECRET", "secret-b")

        assert service_auth.collector_secret(ALIAS) == "secret-a"
        assert service_auth.collector_secret("other-rec") == "secret-b"
        assert service_auth.collector_secret("third-rec") == ""

    def test_a_dotenv_file_is_read_and_does_not_refuse_boot(self, tmp_path):
        """pydantic-settings refuses an unknown dotenv key; these are captured instead.

        @verifies REQ-0035
        """
        env_file = tmp_path / ".env"
        env_file.write_text(f"{ENV_NAME}=from-the-file\n")

        loaded = Settings(_env_file=str(env_file))

        assert loaded.ds_collector_secrets == {ENV_NAME: "from-the-file"}
        assert "from-the-file" not in repr(loaded)

    def test_the_environment_wins_over_the_file(self, monkeypatch):
        """
        @verifies REQ-0035
        """
        monkeypatch.setattr(service_auth.settings, "ds_collector_secrets", {ENV_NAME: "file"})
        assert service_auth.collector_secret(ALIAS) == "file"
        monkeypatch.setenv(ENV_NAME, "environment")
        assert service_auth.collector_secret(ALIAS) == "environment"


# ── one scope per token ───────────────────────────────────────────


class TestOneScopePerToken:
    def test_a_token_is_built_per_alias_and_scope(self, monkeypatch):
        """
        @verifies REQ-0036
        """
        monkeypatch.setenv(ENV_NAME, "s3cret")
        monkeypatch.setenv("SVC_DS_COLLECTOR_OTHER_REC_SECRET", "other")

        write = service_auth.collector_token_provider(ALIAS, service_auth.MEMBERSHIPS_WRITE)
        again = service_auth.collector_token_provider(ALIAS, service_auth.MEMBERSHIPS_WRITE)
        read = service_auth.collector_token_provider(ALIAS, service_auth.CONSENT_COLLECTOR_READ)
        other = service_auth.collector_token_provider("other-rec", service_auth.MEMBERSHIPS_WRITE)

        assert write is again
        assert write is not read and write is not other
        assert (write._client_id, write._scope) == (CLIENT_ID, service_auth.MEMBERSHIPS_WRITE)
        assert other._client_id == "svc-ds-collector-other-rec"

    @pytest.mark.parametrize(
        "scope",
        [
            "identity-registry.memberships.write connector.consent.provision",
            "connector.consent.provision,connector.consent.audience",
            "openid",
            "",
        ],
    )
    def test_anything_but_one_known_scope_is_refused(self, monkeypatch, scope):
        """A multi-scope token carries several audiences, and every receiver refuses it.

        @verifies REQ-0036
        """
        monkeypatch.setenv(ENV_NAME, "s3cret")
        with pytest.raises(ValueError, match="single collector scope"):
            service_auth.collector_token_provider(ALIAS, scope)

    async def test_the_token_request_asks_for_exactly_that_scope(self, monkeypatch):
        """The form posted to the token endpoint, not just the provider's attribute.

        @verifies REQ-0036
        """
        monkeypatch.setenv(ENV_NAME, "s3cret")
        posted: list[dict[str, list[str]]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/.well-known/openid-configuration"):
                return httpx.Response(
                    200,
                    json={
                        "issuer": "http://kc:8080/realms/test",
                        "token_endpoint": "http://kc:8080/realms/test/token",
                        "jwks_uri": "http://kc:8080/realms/test/certs",
                    },
                )
            if req.url.path.endswith("/token"):
                from urllib.parse import parse_qs

                posted.append(parse_qs(req.content.decode()))
                return httpx.Response(200, json={"access_token": "t", "expires_in": 300})
            raise AssertionError(f"unexpected {req.url}")

        _patch_httpx(monkeypatch, handler)

        for scope in (service_auth.MEMBERSHIPS_WRITE, service_auth.CREDENTIALS_WRITE):
            provider = service_auth.collector_token_provider(ALIAS, scope)
            await provider.get_token()

        assert [p["scope"] for p in posted] == [
            [service_auth.MEMBERSHIPS_WRITE],
            [service_auth.CREDENTIALS_WRITE],
        ]
        assert {p["client_id"][0] for p in posted} == {CLIENT_ID}
        assert all(" " not in p["scope"][0] for p in posted)


# ── which scope each call asks for ─────────────────────────────────


class TestEachCallSiteAsksForItsScope:
    """With the collector secret set, every act for the community is the collector's.

    @verifies REQ-0036
    """

    async def test_approval_issues_and_registers_as_the_collector(
        self, monkeypatch, tokens, bound, submission
    ):
        """
        @verifies REQ-0036
        """
        monkeypatch.setenv(ENV_NAME, "s3cret")
        seen: list[tuple[str, str, str]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append((req.method, req.url.path, req.headers.get("authorization")))
            if req.url.path == "/users/resolve":
                return httpx.Response(200, json={"subject_id": "subject-1"})
            if req.url.path == "/admin/credentials/data-subject":
                return httpx.Response(201, json=CREDENTIAL_RESPONSE)
            if req.url.path == "/admin/memberships":
                return httpx.Response(201, json={})
            if req.url.path == "/admin/keycloak/sync":
                return httpx.Response(200, json={})
            raise AssertionError(f"unexpected {req.url}")

        _patch_httpx(monkeypatch, handler)

        await di.provision_user_identity(
            submission, keycloak_user_id="kc-1", keycloak_realm="celine", provision_shares=False
        )

        assert seen == [
            ("POST", "/users/resolve", "Bearer service"),
            ("POST", "/admin/credentials/data-subject", _bearer(service_auth.CREDENTIALS_WRITE)),
            ("POST", "/admin/memberships", _bearer(service_auth.MEMBERSHIPS_WRITE)),
            # The community's act since ds ADR-0026's amendment (2026-10-05).
            ("POST", "/admin/keycloak/sync", _bearer(service_auth.KEYCLOAK_SYNC)),
        ]
        assert {(c, s) for c, _, s in tokens} == {
            (CLIENT_ID, service_auth.CREDENTIALS_WRITE),
            (CLIENT_ID, service_auth.MEMBERSHIPS_WRITE),
            (CLIENT_ID, service_auth.KEYCLOAK_SYNC),
        }

    async def test_revocation_removes_as_the_collector(
        self, monkeypatch, tokens, bound, submission
    ):
        """
        @verifies REQ-0036
        """
        monkeypatch.setenv(ENV_NAME, "s3cret")
        submission.dataspace_vc_id = "urn:uuid:cred-001"
        submission.dataspace_did = "did:web:users.example:subject-1"
        seen: list[tuple[str, str]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append((req.method, req.headers.get("authorization")))
            return httpx.Response(204)

        _patch_httpx(monkeypatch, handler)

        await di.revoke_user_identity(submission)

        assert seen == [
            ("DELETE", _bearer(service_auth.MEMBERSHIPS_WRITE)),
            ("DELETE", _bearer(service_auth.CREDENTIALS_WRITE)),
        ]

    async def test_consent_registration_is_provision(self, monkeypatch, tokens):
        """
        @verifies REQ-0036
        """
        monkeypatch.setenv(ENV_NAME, "s3cret")
        auths = []

        def handler(req):
            auths.append(req.headers.get("authorization"))
            return httpx.Response(200, json=[{"id": "row"}])

        _patch_httpx(monkeypatch, handler)
        route = di.ConsentRoute(offer_id="o", connector_url="http://connector", collector=ALIAS)
        async with httpx.AsyncClient() as client:
            result = await di.register_share(
                client, route, subject_id="did:x", enabled=True, decided_by="subject"
            )

        assert result.ok
        assert auths == [_bearer(service_auth.CONSENT_PROVISION)]

    async def test_the_read_backs_are_collector_read(self, monkeypatch, tokens, bound):
        """subject-shares (both callers) and decisions: the read scope, never a write.

        @verifies REQ-0036
        """
        monkeypatch.setenv(ENV_NAME, "s3cret")
        from celine.onboarding.services import template_service

        template_service._cache["default"]["dataspace"]["connectors"] = [
            {"holder": "example-dso", "url": "http://holder", "offers": ["release"]}
        ]
        seen: list[tuple[str, str]] = []

        def handler(req):
            seen.append((req.url.path, req.headers.get("authorization")))
            if req.url.path.endswith("/decisions"):
                return httpx.Response(
                    200,
                    json={
                        "offer_id": "o",
                        "datasets": ["d"],
                        "subjects": [],
                        "next_cursor": None,
                    },
                )
            return httpx.Response(200, json=[])

        _patch_httpx(monkeypatch, handler)
        route = di.ConsentRoute(offer_id="o", connector_url="http://connector", collector=ALIAS)

        await di.subject_shares_at_holders("default", subject_id="did:x")
        async with httpx.AsyncClient() as client:
            await di._subject_rows(client, route, subject_id="did:x")
        await di.get_offer_decisions("o", [route])

        read = _bearer(service_auth.CONSENT_COLLECTOR_READ)
        assert seen == [
            ("/consent/admin/subject-shares", read),
            ("/consent/admin/subject-shares", read),
            ("/consent/admin/decisions", read),
        ]

    async def test_the_audience_read_is_audience(self, monkeypatch, tokens):
        """
        @verifies REQ-0036
        """
        monkeypatch.setenv(ENV_NAME, "s3cret")
        auths = []

        def handler(req):
            auths.append(req.headers.get("authorization"))
            return httpx.Response(
                200,
                json={
                    "datasets": [{"dataset_id": "d", "subject_ids": ["did:x"], "subject_count": 1}]
                },
            )

        _patch_httpx(monkeypatch, handler)
        route = di.ConsentRoute(offer_id="o", connector_url="http://connector", collector=ALIAS)

        await di.get_offer_audience("o", "did:web:consumer", [route])

        assert auths == [_bearer(service_auth.CONSENT_AUDIENCE)]

    async def test_what_this_service_does_as_itself_stays_its_own(self, monkeypatch, tokens):
        """Resolving an organisation is not an act for a community.

        @verifies REQ-0036
        """
        monkeypatch.setenv(ENV_NAME, "s3cret")
        monkeypatch.setattr(di.settings, "identity_registry_url", "http://ir:30005")
        auths = []

        def handler(req):
            auths.append(req.headers.get("authorization"))
            return httpx.Response(200, json={"id": ALIAS, "status": "verified"})

        _patch_httpx(monkeypatch, handler)
        await di.check_organization(ALIAS)

        assert auths == ["Bearer service"]
        assert tokens == []


# ── the development transition, and the refusal outside it ─────────


class TestTheTransition:
    async def test_in_dev_without_a_secret_the_pre_collector_clients_are_kept(
        self, monkeypatch, tokens, caplog
    ):
        """Consent writes and read-backs as the connector client, the rest as this service.

        @verifies REQ-0037
        """
        monkeypatch.setenv("CELINE_ENV", "dev")
        monkeypatch.setattr(di.settings, "ds_org_client_id", "")
        monkeypatch.setattr(di.settings, "ds_org_client_secret", "org-secret")

        with caplog.at_level(logging.WARNING, logger="celine.onboarding.services.service_auth"):
            provision = await di._collector_headers(ALIAS, service_auth.CONSENT_PROVISION)
            read = await di._collector_headers(ALIAS, service_auth.CONSENT_COLLECTOR_READ)
            membership = await di._collector_headers(ALIAS, service_auth.MEMBERSHIPS_WRITE)
            await di._collector_headers(ALIAS, service_auth.MEMBERSHIPS_WRITE)

        connector = "Bearer svc-ds-connector-example-rec|None"
        assert (provision, read) == ({"Authorization": connector}, {"Authorization": connector})
        assert membership == {"Authorization": "Bearer service"}
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        # Once per alias and scope, naming the variable to set.
        assert len(warnings) == 3
        assert all(ENV_NAME in w for w in warnings)

    @pytest.mark.parametrize("env", ["prod", "staging", ""])
    async def test_outside_dev_a_missing_secret_is_refused_at_the_call(
        self, monkeypatch, tokens, env
    ):
        """
        @verifies REQ-0037
        """
        monkeypatch.setenv("CELINE_ENV", env)
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.setattr(di.settings, "ds_org_client_secret", "org-secret")

        with pytest.raises(ConfigurationError, match=ENV_NAME):
            await di._collector_headers(ALIAS, service_auth.MEMBERSHIPS_WRITE)
        assert tokens == []

    async def test_with_a_secret_dev_uses_the_collector_too(self, monkeypatch, tokens):
        """The transition is for a community without a secret, not for dev as such.

        @verifies REQ-0037
        """
        monkeypatch.setenv("CELINE_ENV", "dev")
        monkeypatch.setenv(ENV_NAME, "s3cret")

        headers = await di._collector_headers(ALIAS, service_auth.CONSENT_PROVISION)

        assert headers == {"Authorization": _bearer(service_auth.CONSENT_PROVISION)}


class TestTheBootCheck:
    def test_outside_dev_a_bound_community_without_its_secret_refuses_boot(
        self, monkeypatch, bound
    ):
        """
        @verifies REQ-0037
        """
        with pytest.raises(InsecureConfiguration, match=ENV_NAME):
            service_auth.enforce_collector_posture(env="prod")

    def test_outside_dev_the_client_id_as_secret_refuses_boot(self, monkeypatch, bound):
        """
        @verifies REQ-0037
        """
        monkeypatch.setenv(ENV_NAME, CLIENT_ID)
        with pytest.raises(InsecureConfiguration, match=ENV_NAME):
            service_auth.enforce_collector_posture(env="prod")

    def test_every_bound_community_is_named_in_one_message(self, monkeypatch, bound, bind_rec):
        """
        @verifies REQ-0037
        """
        bind_rec("second", organization="other-rec")
        guard = service_auth.collector_posture_guard(env="prod")
        assert sorted(v.setting for v in guard.violations) == [
            ENV_NAME,
            "SVC_DS_COLLECTOR_OTHER_REC_SECRET",
        ]

    def test_a_real_secret_passes(self, monkeypatch, bound):
        """
        @verifies REQ-0037
        """
        monkeypatch.setenv(ENV_NAME, "a-real-secret")
        service_auth.enforce_collector_posture(env="prod")

    def test_an_unbound_community_or_a_switched_off_dataspace_needs_none(
        self, monkeypatch, bind_rec
    ):
        """
        @verifies REQ-0037
        """
        bind_rec("default")  # no dataspace block
        monkeypatch.setattr(di.settings, "dataspace_enabled", True)
        assert service_auth.collector_posture_guard(env="prod").violations == []

        bind_rec("default", organization=ALIAS)
        monkeypatch.setattr(di.settings, "dataspace_enabled", False)
        assert service_auth.collector_posture_guard(env="prod").violations == []

    def test_in_dev_it_is_a_warning_and_boot_proceeds(self, monkeypatch, bound, caplog):
        """
        @verifies REQ-0037
        """
        with caplog.at_level(logging.WARNING):
            service_auth.enforce_collector_posture(env="dev")
        assert ENV_NAME in caplog.text


# ── a refused membership delete ──────────────────────────────────


class TestTheMembershipDelete:
    @pytest.mark.parametrize("status", [204, 200, 404])
    async def test_gone_is_success(self, monkeypatch, status):
        """
        @verifies REQ-0038
        """
        _patch_httpx(monkeypatch, lambda req: httpx.Response(status))
        await di._delete_membership("http://ir", {}, "did:x", ALIAS)

    @pytest.mark.parametrize("status", [400, 401, 403, 409, 500, 503])
    async def test_a_refusal_raises(self, monkeypatch, status):
        """It used to be swallowed, and revocation reported success over a row left behind.

        @verifies REQ-0038
        """
        _patch_httpx(monkeypatch, lambda req: httpx.Response(status, text="no"))
        with pytest.raises(ValueError, match=f"failed \\({status}\\)"):
            await di._delete_membership("http://ir", {}, "did:x", ALIAS)

    async def test_an_unreachable_registry_raises(self, monkeypatch):
        """
        @verifies REQ-0038
        """

        def handler(req):
            raise httpx.ConnectError("down")

        _patch_httpx(monkeypatch, handler)
        with pytest.raises(ValueError, match="could not reach"):
            await di._delete_membership("http://ir", {}, "did:x", ALIAS)

    async def test_revocation_stops_and_keeps_the_record_when_the_delete_is_refused(
        self, monkeypatch, tokens, bound, submission
    ):
        """
        @verifies REQ-0038
        """
        monkeypatch.setenv(ENV_NAME, "s3cret")
        submission.dataspace_vc_id = "urn:uuid:cred-001"
        submission.dataspace_did = "did:web:users.example:subject-1"
        calls = []

        def handler(req):
            calls.append(req.url.path)
            return httpx.Response(403, text="not your organisation")

        _patch_httpx(monkeypatch, handler)

        with pytest.raises(ValueError, match="403"):
            await di.revoke_user_identity(submission)

        assert calls == [f"/admin/memberships/{submission.dataspace_did}/{ALIAS}"]
        assert submission.dataspace_vc_id == "urn:uuid:cred-001"

    async def test_the_rollback_names_both_failures_and_keeps_the_cause(self, monkeypatch):
        """The Keycloak failure stays the cause; the membership left behind is named too.

        @verifies REQ-0038
        """
        di._token_provider = _named_token_provider("service")
        calls = []

        def handler(req):
            calls.append((req.method, req.url.path))
            if req.url.path == "/admin/keycloak/sync":
                return httpx.Response(500, text="keycloak down")
            if req.url.path.startswith("/admin/memberships/"):
                return httpx.Response(403, text="refused")
            return httpx.Response(204)

        _patch_httpx(monkeypatch, handler)

        with pytest.raises(ValueError) as raised:
            await di._sync_keycloak(
                "http://ir",
                {},
                did="did:x",
                keycloak_user_id="kc-1",
                keycloak_realm="celine",
                email=None,
                credential_id="urn:uuid:cred-001",
                organization_alias=ALIAS,
            )

        message = str(raised.value)
        assert "Keycloak sync failed" in message
        assert "membership of did:x" in message and "403" in message
        assert "has been revoked" not in message
        # The original failure is the cause, not the rollback's.
        assert "KC sync failed (500)" in str(raised.value.__cause__)
        # The credential is still revoked: one undo failing does not stop the other.
        assert ("DELETE", "/admin/credentials/urn:uuid:cred-001") in calls

    async def test_a_clean_rollback_still_says_the_credential_was_revoked(self, monkeypatch):
        """
        @verifies REQ-0038
        """
        di._token_provider = _named_token_provider("service")

        def handler(req):
            if req.url.path == "/admin/keycloak/sync":
                return httpx.Response(500, text="keycloak down")
            return httpx.Response(204)

        _patch_httpx(monkeypatch, handler)

        with pytest.raises(ValueError, match="has been revoked") as raised:
            await di._sync_keycloak(
                "http://ir",
                {},
                did="did:x",
                keycloak_user_id="kc-1",
                keycloak_realm="celine",
                email=None,
                credential_id="urn:uuid:cred-001",
                organization_alias=ALIAS,
            )
        assert "KC sync failed (500)" in str(raised.value.__cause__)


# ── the contract inventory says the same ───────────────────────────


class TestTheInventory:
    """`tests/contract/inventory.py` is the declaration of every call; its principal
    and scope column must say what the code does.

    @verifies REQ-0036
    """

    def test_every_collector_call_names_one_known_scope(self):
        """
        @verifies REQ-0036
        """
        from contract.inventory import CALLS

        for call in CALLS:
            assert call.acts_as in {"service", "collector", "member"}, call
            if call.acts_as == "collector":
                assert call.scope in service_auth.COLLECTOR_SCOPES, call
            else:
                assert call.scope is None, call

    def test_the_scopes_are_the_ones_the_code_asks_for(self):
        """The same table the call-site tests above prove, read off the inventory.

        @verifies REQ-0036
        """
        from contract.inventory import CALLS

        declared = {(c.method, c.path): c.scope for c in CALLS if c.acts_as == "collector"}
        assert declared == {
            ("post", "/admin/credentials/data-subject"): service_auth.CREDENTIALS_WRITE,
            ("delete", "/admin/credentials/{cred_id}"): service_auth.CREDENTIALS_WRITE,
            ("post", "/admin/memberships"): service_auth.MEMBERSHIPS_WRITE,
            (
                "delete",
                "/admin/memberships/{user_did}/{organization_alias}",
            ): service_auth.MEMBERSHIPS_WRITE,
            ("post", "/admin/keycloak/sync"): service_auth.KEYCLOAK_SYNC,
            ("post", "/consent/admin/shares"): service_auth.CONSENT_PROVISION,
            ("get", "/consent/admin/subject-shares"): service_auth.CONSENT_COLLECTOR_READ,
            ("get", "/consent/admin/decisions"): service_auth.CONSENT_COLLECTOR_READ,
            ("get", "/consent/admin/shares"): service_auth.CONSENT_AUDIENCE,
        }
        members = {(c.method, c.path) for c in CALLS if c.acts_as == "member"}
        assert members == {
            ("get", "/consent/my/shares"),
            ("post", "/consent/my/shares"),
            ("get", "/prov/my/events"),
        }
