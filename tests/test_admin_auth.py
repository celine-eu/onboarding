"""The admin surface authenticates operators and refuses everyone else.

Tokens here are really signed and really verified — issuer, audience, expiry and
signature all go through the same `JwtUser.from_token` path production uses, with
only the key source swapped. The point of the suite is the two answers that used
to be impossible: *who* is calling, and *which community* they may act on.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from celine.onboarding.api.admin import create_admin_router
from celine.onboarding.security.middleware import AdminAuthMiddleware

ORG = "community-a"
OTHER_ORG = "community-b"


@pytest.fixture()
def recs(seed_rec):
    seed_rec("rec-a", name="REC A", organization=ORG)
    seed_rec("rec-b", name="REC B", organization=OTHER_ORG)
    seed_rec("orphan", name="Unbound REC")


@pytest.fixture()
def client(recs, issue_token) -> TestClient:
    """`issue_token` is requested so its monkeypatched OIDC config is in place."""
    app = FastAPI()
    app.add_middleware(AdminAuthMiddleware)
    app.include_router(create_admin_router())
    return TestClient(app)


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuthentication:
    def test_no_token_is_401(self, client):
        assert client.get("/api/admin/me").status_code == 401

    def test_garbage_token_is_401(self, client):
        assert client.get("/api/admin/me", headers=auth("not-a-jwt")).status_code == 401

    def test_expired_token_is_401(self, client, issue_token):
        token = issue_token(exp=int(time.time()) - 3600)
        assert client.get("/api/admin/me", headers=auth(token)).status_code == 401

    def test_wrong_audience_is_401(self, client, issue_token):
        """A token minted for another CELINE service must not work here.

        This is what the oauth2-proxy audience mapper on `svc-onboarding` buys.
        """
        token = issue_token(aud="svc-grid")
        assert client.get("/api/admin/me", headers=auth(token)).status_code == 401

    def test_wrong_issuer_is_401(self, client, issue_token):
        token = issue_token(iss="https://evil.test/realms/celine")
        assert client.get("/api/admin/me", headers=auth(token)).status_code == 401

    def test_oauth2_proxy_header_is_accepted(self, client, operator_token):
        """How a browser session arrives — Caddy forward_auth copies this header."""
        token = operator_token(ORG, "viewers")
        response = client.get("/api/admin/me", headers={"X-Auth-Request-Access-Token": token})
        assert response.status_code == 200

    def test_unsigned_token_is_rejected(self, client, issue_token):
        """`alg: none` must not bypass verification."""
        import jwt as pyjwt

        forged = pyjwt.encode(
            {
                "iss": "https://keycloak.test/realms/celine",
                "aud": "svc-onboarding",
                "sub": "attacker",
                "exp": int(time.time()) + 300,
                "groups": ["/admins"],
            },
            key="",
            algorithm="none",
        )
        assert client.get("/api/admin/me", headers=auth(forged)).status_code == 401


# ---------------------------------------------------------------------------
# /me
# ---------------------------------------------------------------------------


class TestMe:
    def test_operator_sees_only_their_community(self, client, operator_token):
        body = client.get("/api/admin/me", headers=auth(operator_token(ORG, "managers"))).json()
        assert [r["slug"] for r in body["recs"]] == ["rec-a"]
        assert body["recs"][0]["organization"] == ORG
        assert "submissions.review" in body["recs"][0]["capabilities"]

    def test_capabilities_match_the_tier(self, client, operator_token):
        body = client.get("/api/admin/me", headers=auth(operator_token(ORG, "viewers"))).json()
        capabilities = body["recs"][0]["capabilities"]
        assert "submissions.read" in capabilities
        assert "submissions.review" not in capabilities
        assert "submissions.purge" not in capabilities

    def test_identity_is_reported(self, client, operator_token):
        body = client.get("/api/admin/me", headers=auth(operator_token(ORG, "editors"))).json()
        assert body["sub"] == "operator-sub"
        assert body["email"] == "operator@example.org"
        assert body["subject_type"] == "user"
        assert body["organizations"] == [ORG]

    def test_realm_operator_sees_every_community(self, client, operator_token):
        token = operator_token(ORG, realm=("admins",))
        body = client.get("/api/admin/me", headers=auth(token)).json()
        # Including the REC that declares no organization at all — a realm grant
        # is platform-wide, which is exactly how such a REC stays administrable.
        assert [r["slug"] for r in body["recs"]] == ["orphan", "rec-a", "rec-b"]

    def test_operator_of_nothing_is_403(self, client, issue_token):
        """Signed in but granted nothing. The console shows a denied page.

        200-with-an-empty-list would send it back to a login it has already passed.
        """
        token = issue_token(email="nobody@example.org", preferred_username="nobody")
        response = client.get("/api/admin/me", headers=auth(token))
        assert response.status_code == 403
        assert "Keycloak organization" in response.json()["detail"]

    def test_unbound_rec_is_invisible_to_an_org_operator(self, client, operator_token):
        body = client.get("/api/admin/me", headers=auth(operator_token(ORG, "admins"))).json()
        assert "orphan" not in [r["slug"] for r in body["recs"]]

    def test_service_account_is_reported_as_such(self, client, service_token):
        body = client.get("/api/admin/me", headers=auth(service_token("onboarding.admin"))).json()
        assert body["subject_type"] == "service"
        assert body["organizations"] == []


# ---------------------------------------------------------------------------
# /recs
# ---------------------------------------------------------------------------


class TestRecs:
    def test_lists_only_accessible_communities(self, client, operator_token):
        body = client.get(
            "/api/admin/recs", headers=auth(operator_token(OTHER_ORG, "viewers"))
        ).json()
        assert [r["slug"] for r in body] == ["rec-b"]

    def test_empty_list_rather_than_403(self, client, issue_token):
        """A picker with nothing in it is a legitimate answer; `/me` said the rest."""
        token = issue_token(email="nobody@example.org")
        response = client.get("/api/admin/recs", headers=auth(token))
        assert response.status_code == 200
        assert response.json() == []

    def test_reload_needs_a_platform_operator(self, client, operator_token):
        """Deployment-wide, so an organization grant cannot reach it."""
        org_admin = operator_token(ORG, "admins")
        assert client.post("/api/admin/recs/reload", headers=auth(org_admin)).status_code == 403

    def test_reload_allows_a_realm_operator(self, client, operator_token, monkeypatch):
        from celine.onboarding.services import template_service

        async def _noop_reload():
            return None

        monkeypatch.setattr(template_service, "reload", _noop_reload)
        token = operator_token(ORG, realm=("viewers",))
        assert client.post("/api/admin/recs/reload", headers=auth(token)).status_code == 200

    def test_recs_is_not_shadowed_by_the_rec_slug_route(self, client, operator_token):
        """`/api/admin/recs` is the community list, not a REC named "recs"."""
        response = client.get("/api/admin/recs", headers=auth(operator_token(ORG, "viewers")))
        assert response.status_code == 200
        assert isinstance(response.json(), list)


# ---------------------------------------------------------------------------
# Tenancy on the REC-scoped endpoints
# ---------------------------------------------------------------------------


class TestTenancy:
    def test_unknown_rec_is_404(self, client, operator_token):
        token = operator_token(ORG, "admins", realm=("admins",))
        assert client.get("/api/admin/nope/submissions", headers=auth(token)).status_code == 404

    def test_operator_is_403_on_another_community(self, client, operator_token):
        response = client.get(
            "/api/admin/rec-b/submissions", headers=auth(operator_token(ORG, "admins"))
        )
        assert response.status_code == 403
        assert "different organization" in response.json()["detail"]

    def test_viewer_cannot_erase(self, client, operator_token):
        response = client.delete(
            "/api/admin/rec-a/submissions/00000000-0000-0000-0000-000000000000",
            headers=auth(operator_token(ORG, "viewers")),
        )
        assert response.status_code == 403

    def test_manager_cannot_erase(self, client, operator_token):
        """Erasure is separated from review on purpose: it is not recoverable."""
        response = client.delete(
            "/api/admin/rec-a/submissions/00000000-0000-0000-0000-000000000000",
            headers=auth(operator_token(ORG, "managers")),
        )
        assert response.status_code == 403

    def test_service_account_scope_is_enough(self, client, service_token, monkeypatch):
        """No organization to check — a scoped service may act on any community."""
        from celine.onboarding.services import submission_service

        async def _empty(db, **kwargs):
            return []

        monkeypatch.setattr(submission_service, "list_submissions", _empty)

        async def _noop(*a, **k):
            return None

        monkeypatch.setattr("celine.onboarding.services.audit_service.record_and_commit", _noop)
        response = client.get(
            "/api/admin/rec-a/submissions",
            headers=auth(service_token("onboarding.submissions.read")),
        )
        assert response.status_code == 200

    def test_narrow_service_scope_does_not_reach_other_endpoints(self, client, service_token):
        response = client.delete(
            "/api/admin/rec-a/submissions/00000000-0000-0000-0000-000000000000",
            headers=auth(service_token("onboarding.submissions.read")),
        )
        assert response.status_code == 403
