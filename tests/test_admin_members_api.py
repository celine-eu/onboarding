"""A manager emails a registry member through onboarding, by member key.

The routes are a delegated call in front of one provisioning route, so what is
pinned here is the seam on both sides: who may call (a service holding
`onboarding.members.invite`, carrying a manager's verified token), which REC the
registry community resolves to, what reaches the provisioning service, what its
answers become, and the one row this service writes.

`respx` sits in front of the real `celine.sdk.provisioning` client, as in
`tests/test_provisioning.py`, so the assertions are about the wire contract rather
than a stub. Tokens are minted and verified for real (`issue_token`).
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from celine.onboarding.api.admin import create_admin_router
from celine.onboarding.api.admin.deps import RESERVED_SLUGS
from celine.onboarding.models.audit_log import AuditLog
from celine.onboarding.models.database import get_db
from celine.onboarding.security.middleware import AdminAuthMiddleware
from celine.onboarding.services import provisioning as pv

PROVISIONING = "http://provisioning.test"
ORG = "greenland"
COMMUNITY = "greenland"
OTHER_ORG = "blueland"
MEMBER = "M-0042"

INVITE = f"/api/admin/communities/{COMMUNITY}/members/{MEMBER}/invitation"
RESET = f"/api/admin/communities/{COMMUNITY}/members/{MEMBER}/password-reset"
UPSTREAM = f"/participants/{COMMUNITY}/{MEMBER}/invitation"


class RecordingSession:
    """Enough `AsyncSession` to see the audit row, and to prove nothing is queried.

    No submission may be read: the member these routes address may have none. A
    query of any kind fails the request, and so the test.
    """

    def __init__(self) -> None:
        self.added: list = []
        self.commits = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, *args, **kwargs):  # pragma: no cover - the failure path
        raise AssertionError("the member routes must not query the database")

    scalar = scalars = get = execute

    @property
    def audit_rows(self) -> list[AuditLog]:
        return [row for row in self.added if isinstance(row, AuditLog)]


@pytest.fixture()
def db() -> RecordingSession:
    return RecordingSession()


@pytest.fixture()
def recs(seed_rec):
    # A slug that is not the community key: the routes must not depend on it.
    seed_rec(
        "green-rec",
        organization=ORG,
        rec_registry={"community": COMMUNITY, "default_area": "north"},
    )
    seed_rec(
        "blue-rec",
        organization=OTHER_ORG,
        rec_registry={"community": OTHER_ORG, "default_area": "north"},
    )
    seed_rec("unbound", organization="nowhere")


@pytest.fixture()
def enabled(monkeypatch):
    from celine.sdk.provisioning import ProvisioningClient

    monkeypatch.setattr(pv.settings, "provisioning_url", PROVISIONING)
    pv.reset_client()
    monkeypatch.setattr(
        pv, "_client", ProvisioningClient(base_url=PROVISIONING, default_token="svc-onboarding")
    )
    yield
    pv.reset_client()


@pytest.fixture()
def api():
    with respx.mock(base_url=PROVISIONING, assert_all_called=False) as mock:
        yield mock


@pytest.fixture()
def client(recs, issue_token, db) -> TestClient:
    app = FastAPI()
    app.add_middleware(AdminAuthMiddleware)
    app.include_router(create_admin_router())

    async def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


@pytest.fixture()
def community_token(service_token):
    return service_token("onboarding.members.invite", client_id="svc-community")


@pytest.fixture()
def realm_community_token(issue_token):
    """`svc-community`'s token as a realm synced by celine-policies issues it.

    Measured on the local stack, 2026-09-14: the sync assigns exactly the declared
    scopes, so Keycloak's built-in `service_account` scope is absent and the token
    carries **no `client_id` and no `preferred_username`**. It has `azp`, `sub`,
    `scope`, and a `jti` whose `trrtcc:` prefix records the client-credentials
    grant. The `service_token` fixture carries both missing claims, and so it hid
    the first end-to-end refusal.
    """
    return issue_token(
        sub="0c7a3c3e-5d0b-4d1e-9a39-6f7f2b1c0a11",
        azp="svc-community",
        scope="onboarding.members.invite",
        jti="trrtcc:8b4f2a64-1f61-4c38-9d6e-2a5f5b7e9c01",
    )


@pytest.fixture()
def manager_token(operator_token):
    return operator_token(ORG, "managers", sub="manager-sub", email="manager@example.org")


def delegated(service: str, actor: str | None) -> dict:
    headers = {"Authorization": f"Bearer {service}"}
    if actor is not None:
        headers["X-Acting-User-Token"] = actor
    return headers


def sent(lifespan: int = 604800, invitation: str = "sent") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "user_id": "kc-uuid",
            "username": "member@example.org",
            "actions": ["UPDATE_PASSWORD"],
            "lifespan": lifespan,
            "invitation": invitation,
        },
    )


def refused(status: int, code: str, **headers: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"detail": {"code": code, "message": "the service's own words, never relayed"}},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# What reaches the provisioning service
# ---------------------------------------------------------------------------


class TestTheCall:
    @pytest.mark.parametrize("path,intent", [(INVITE, "invitation"), (RESET, "password_reset")])
    def test_both_path_values_and_the_routes_intent_go_through_unchanged(
        self, client, enabled, api, community_token, manager_token, path, intent
    ):
        route = api.post(UPSTREAM).mock(return_value=sent())

        response = client.post(path, headers=delegated(community_token, manager_token))

        assert response.status_code == 200, response.text
        assert route.call_count == 1
        assert json.loads(route.calls.last.request.content) == {"intent": intent}
        assert response.json()["kind"] == intent

    def test_the_request_body_is_ignored(
        self, client, enabled, api, community_token, manager_token
    ):
        """The intent is the route, so the reset route cannot be made to invite."""
        route = api.post(UPSTREAM).mock(return_value=sent())
        client.post(
            RESET,
            headers=delegated(community_token, manager_token),
            json={"intent": "invitation"},
        )
        assert json.loads(route.calls.last.request.content) == {"intent": "password_reset"}

    def test_onboardings_own_client_token_is_presented(
        self, client, enabled, api, community_token, manager_token
    ):
        route = api.post(UPSTREAM).mock(return_value=sent())
        client.post(INVITE, headers=delegated(community_token, manager_token))
        authorization = route.calls.last.request.headers["authorization"]
        assert authorization == "Bearer svc-onboarding"
        assert manager_token not in str(route.calls.last.request.headers)

    def test_no_submission_is_read(self, client, enabled, api, community_token, manager_token, db):
        """`RecordingSession` fails on any query; a registry-only member succeeds."""
        api.post(UPSTREAM).mock(return_value=sent())
        response = client.post(INVITE, headers=delegated(community_token, manager_token))
        assert response.status_code == 200
        assert len(db.added) == 1


# ---------------------------------------------------------------------------
# What its answers become
# ---------------------------------------------------------------------------


class TestTheCodes:
    def test_sent(self, client, enabled, api, community_token, manager_token):
        api.post(UPSTREAM).mock(return_value=sent(lifespan=3600))
        response = client.post(RESET, headers=delegated(community_token, manager_token))
        assert response.status_code == 200
        assert response.json() == {
            "code": "sent",
            "kind": "password_reset",
            "lifespanSeconds": 3600,
        }

    def test_not_on_dev_list_is_a_200(self, client, enabled, api, community_token, manager_token):
        api.post(UPSTREAM).mock(return_value=sent(invitation="not_on_dev_list"))
        response = client.post(INVITE, headers=delegated(community_token, manager_token))
        assert response.status_code == 200
        assert response.json()["code"] == "not_on_dev_list"

    @pytest.mark.parametrize(
        "status,code",
        [
            (404, "community_not_found"),
            (404, "member_not_found"),
            (404, "account_not_found"),
            (409, "account_disabled"),
            (409, "no_email"),
            (409, "has_password"),
            (409, "no_password"),
            (502, "send_failed"),
            (502, "registry_unavailable"),
            (502, "provisioning_failed"),
            # A code this service has never heard of keeps its status and its code.
            (409, "a_code_from_the_future"),
            (418, "teapot"),
        ],
    )
    def test_a_refusal_passes_through_with_its_status_and_code(
        self, client, enabled, api, community_token, manager_token, status, code
    ):
        api.post(UPSTREAM).mock(return_value=refused(status, code))
        response = client.post(INVITE, headers=delegated(community_token, manager_token))
        assert response.status_code == status
        detail = response.json()["detail"]
        assert detail["code"] == code
        assert "never relayed" not in detail["message"]

    def test_cooldown_carries_the_wait(self, client, enabled, api, community_token, manager_token):
        api.post(UPSTREAM).mock(return_value=refused(429, "cooldown", **{"Retry-After": "240"}))
        response = client.post(INVITE, headers=delegated(community_token, manager_token))
        assert response.status_code == 429
        assert response.json()["detail"]["code"] == "cooldown"
        assert response.json()["detail"]["retryAfterSeconds"] == 240
        assert response.headers["retry-after"] == "240"

    @pytest.mark.parametrize("status,code", [(401, "invalid_token"), (403, "insufficient_scope")])
    def test_onboardings_credential_refused_is_a_deployment_fault(
        self, client, enabled, api, community_token, manager_token, status, code, caplog
    ):
        api.post(UPSTREAM).mock(return_value=refused(status, code))
        with caplog.at_level("ERROR"):
            response = client.post(INVITE, headers=delegated(community_token, manager_token))
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "provisioning_refused"
        assert "provisioning.participants.write" in caplog.text

    @pytest.mark.parametrize("error", [httpx.ConnectTimeout("slow"), httpx.ConnectError("refused")])
    def test_an_unreachable_service_is_a_503(
        self, client, enabled, api, community_token, manager_token, error
    ):
        api.post(UPSTREAM).mock(side_effect=error)
        response = client.post(INVITE, headers=delegated(community_token, manager_token))
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "provisioning_unavailable"

    def test_no_provisioning_service_is_a_503_with_no_call(
        self, client, api, community_token, manager_token, monkeypatch
    ):
        monkeypatch.setattr(pv.settings, "provisioning_url", "")
        route = api.post(UPSTREAM).mock(return_value=sent())
        response = client.post(INVITE, headers=delegated(community_token, manager_token))
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "provisioning_not_configured"
        assert route.call_count == 0


# ---------------------------------------------------------------------------
# Which REC the community resolves to
# ---------------------------------------------------------------------------


class TestTheCommunity:
    def test_a_community_no_manifest_declares_is_not_served(
        self, client, enabled, api, community_token, manager_token
    ):
        route = api.post(url__regex=r".*").mock(return_value=sent())
        response = client.post(
            f"/api/admin/communities/nowhere/members/{MEMBER}/invitation",
            headers=delegated(community_token, manager_token),
        )
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "community_not_served"
        assert route.call_count == 0

    def test_the_slug_is_not_the_key(self, client, enabled, api, community_token, manager_token):
        response = client.post(
            f"/api/admin/communities/green-rec/members/{MEMBER}/invitation",
            headers=delegated(community_token, manager_token),
        )
        assert response.json()["detail"]["code"] == "community_not_served"

    def test_two_manifests_in_one_community_is_ambiguous(
        self, client, enabled, api, community_token, manager_token, seed_rec, caplog
    ):
        seed_rec(
            "green-rec-2",
            organization=ORG,
            rec_registry={"community": COMMUNITY, "default_area": "north"},
        )
        route = api.post(UPSTREAM).mock(return_value=sent())
        with caplog.at_level("ERROR"):
            response = client.post(INVITE, headers=delegated(community_token, manager_token))
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "community_ambiguous"
        assert route.call_count == 0
        assert "green-rec, green-rec-2" in caplog.text

    def test_a_malformed_registry_block_elsewhere_does_not_break_resolution(
        self, client, enabled, api, community_token, manager_token, seed_rec
    ):
        seed_rec("broken", organization="broken", rec_registry="not a mapping")
        api.post(UPSTREAM).mock(return_value=sent())
        response = client.post(INVITE, headers=delegated(community_token, manager_token))
        assert response.status_code == 200

    def test_communities_is_a_reserved_slug(self):
        assert "communities" in RESERVED_SLUGS


# ---------------------------------------------------------------------------
# Who may call
# ---------------------------------------------------------------------------


class TestWhoMayCall:
    def test_a_manager_of_another_organization_is_denied(
        self, client, enabled, api, community_token, operator_token, db
    ):
        route = api.post(UPSTREAM).mock(return_value=sent())
        outsider = operator_token(OTHER_ORG, "managers")
        response = client.post(INVITE, headers=delegated(community_token, outsider))
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "forbidden"
        assert route.call_count == 0
        assert db.audit_rows == []

    def test_an_editor_is_denied(self, client, enabled, api, community_token, operator_token):
        response = client.post(
            INVITE, headers=delegated(community_token, operator_token(ORG, "editors"))
        )
        assert response.status_code == 403

    def test_a_realm_manager_is_allowed(
        self, client, enabled, api, community_token, operator_token
    ):
        api.post(UPSTREAM).mock(return_value=sent())
        platform = operator_token("elsewhere", realm=("managers",))
        response = client.post(INVITE, headers=delegated(community_token, platform))
        assert response.status_code == 200

    def test_a_managers_own_token_is_denied(self, client, enabled, api, manager_token, db):
        route = api.post(UPSTREAM).mock(return_value=sent())
        response = client.post(INVITE, headers=delegated(manager_token, manager_token))
        assert response.status_code == 403
        assert route.call_count == 0
        assert db.audit_rows == []

    def test_a_service_without_the_scope_is_denied(
        self, client, enabled, api, service_token, manager_token
    ):
        other = service_token("onboarding.submissions.review", client_id="svc-community")
        response = client.post(INVITE, headers=delegated(other, manager_token))
        assert response.status_code == 403
        assert "missing a scope" in response.json()["detail"]["message"]

    def test_onboarding_admin_alone_cannot_send(self, client, enabled, api, service_token, db):
        admin = service_token("onboarding.admin")
        response = client.post(INVITE, headers=delegated(admin, admin))
        assert response.status_code == 403
        assert db.audit_rows == []

    def test_a_missing_actor_token_is_a_broken_caller(
        self, client, enabled, api, community_token, db
    ):
        response = client.post(INVITE, headers=delegated(community_token, None))
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "actor_token_invalid"
        assert db.audit_rows == []

    def test_an_invalid_actor_token_is_a_broken_caller(
        self, client, enabled, api, community_token, issue_token
    ):
        expired = issue_token(exp=1, email="manager@example.org")
        for actor in ("not-a-jwt", expired):
            response = client.post(INVITE, headers=delegated(community_token, actor))
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "actor_token_invalid"

    def test_an_invalid_service_token_is_refused(self, client, enabled, manager_token):
        response = client.post(INVITE, headers=delegated("not-a-jwt", manager_token))
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "invalid_token"

    def test_the_oauth2_proxy_header_is_refused(
        self, client, enabled, api, community_token, manager_token, db
    ):
        """That header is read first elsewhere; here it would skip the scope check."""
        route = api.post(UPSTREAM).mock(return_value=sent())
        for headers in (
            {
                **delegated(community_token, manager_token),
                "x-auth-request-access-token": manager_token,
            },
            {"x-auth-request-access-token": manager_token, "X-Acting-User-Token": manager_token},
        ):
            response = client.post(INVITE, headers=headers)
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "proxy_token_refused"
        assert route.call_count == 0
        assert db.audit_rows == []

    def test_no_token_at_all_is_401(self, client):
        assert client.post(INVITE).status_code == 401


class TestARealmIssuedServiceToken:
    """The token shape a real realm issues, not the fixture's.

    Passing depends on `celine.sdk.auth.is_service_account` reading Keycloak's
    grant marker in `jti`. That code is in the editable `../celine-sdk` checkout
    and was not in 1.19.0.
    """

    def test_it_is_a_service_and_the_delegated_call_goes_through(
        self, client, enabled, api, realm_community_token, manager_token, db
    ):
        route = api.post(UPSTREAM).mock(return_value=sent())

        response = client.post(INVITE, headers=delegated(realm_community_token, manager_token))

        assert response.status_code == 200, response.text
        assert route.call_count == 1
        [row] = db.audit_rows
        # From `azp`, the only client claim such a token carries.
        assert row.actor_client_id == "svc-community"
        assert row.actor_sub == "manager-sub"

    def test_a_managers_password_grant_token_is_still_a_person(
        self, client, enabled, api, operator_token
    ):
        """The marker must not turn a person into a service: `onrtro:` is a password grant."""
        route = api.post(UPSTREAM).mock(return_value=sent())
        manager = operator_token(ORG, "managers", jti="onrtro:5f0e1f7c-1111-4a4a-9b9b-0c0c0c0c0c0c")

        response = client.post(INVITE, headers=delegated(manager, manager))

        assert response.status_code == 403
        assert "only through a service" in response.json()["detail"]["message"]
        assert route.call_count == 0


# ---------------------------------------------------------------------------
# The audit row
# ---------------------------------------------------------------------------


class TestTheAuditRow:
    @pytest.mark.parametrize(
        "path,action", [(INVITE, "member_invitation"), (RESET, "member_password_reset")]
    )
    def test_one_row_names_the_manager_through_the_community_service(
        self, client, enabled, api, community_token, manager_token, db, path, action
    ):
        api.post(UPSTREAM).mock(return_value=sent())
        client.post(path, headers=delegated(community_token, manager_token))

        [row] = db.audit_rows
        assert db.commits == 1
        assert row.action == action
        assert row.entity_type == "registry_member"
        assert row.entity_id == MEMBER
        assert row.rec_slug == "green-rec"
        assert row.actor_type == "user"
        assert row.actor_sub == "manager-sub"
        assert row.actor_email == "manager@example.org"
        assert row.actor_client_id == "svc-community"
        assert row.detail == f"community={COMMUNITY} code=sent status=200"

    def test_a_provisioning_refusal_is_audited(
        self, client, enabled, api, community_token, manager_token, db
    ):
        api.post(UPSTREAM).mock(return_value=refused(409, "no_email"))
        client.post(INVITE, headers=delegated(community_token, manager_token))
        [row] = db.audit_rows
        assert row.detail == f"community={COMMUNITY} code=no_email status=409"

    def test_an_unreachable_service_is_audited_with_no_status(
        self, client, enabled, api, community_token, manager_token, db
    ):
        api.post(UPSTREAM).mock(side_effect=httpx.ConnectError("refused"))
        client.post(INVITE, headers=delegated(community_token, manager_token))
        [row] = db.audit_rows
        assert row.detail == f"community={COMMUNITY} code=provisioning_unavailable status=none"

    def test_the_row_holds_no_name_and_no_address(
        self, client, enabled, api, community_token, manager_token, db
    ):
        api.post(UPSTREAM).mock(return_value=sent())
        client.post(INVITE, headers=delegated(community_token, manager_token))
        [row] = db.audit_rows
        assert "member@example.org" not in (row.detail or "")
