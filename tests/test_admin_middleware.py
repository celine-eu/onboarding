"""The admin-path auth pre-check.

Two things matter here: the public wizard must stay reachable with no
credentials, and the admin surface must answer 401 — not a redirect — when they
are absent.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from celine.onboarding.security.middleware import AdminAuthMiddleware, is_admin_path


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.add_middleware(AdminAuthMiddleware)

    @app.get("/api/config")
    async def public() -> dict:
        return {"public": True}

    @app.get("/api/admin/ping")
    async def admin_ping() -> dict:
        return {"reached": True}

    @app.get("/api/administrators")
    async def lookalike() -> dict:
        return {"reached": True}

    return TestClient(app)


class TestPathMatching:
    def test_admin_paths(self):
        assert is_admin_path("/api/admin")
        assert is_admin_path("/api/admin/")
        assert is_admin_path("/api/admin/ping")
        assert is_admin_path("/api/admin/my-rec/submissions")

    def test_lookalikes_are_not_admin_paths(self):
        """A bare prefix match would swallow these; the Caddy matcher needs the
        same care."""
        assert not is_admin_path("/api/administrators")
        assert not is_admin_path("/api/adminx")
        assert not is_admin_path("/api/config")
        assert not is_admin_path("/")


class TestGate:
    def test_public_paths_need_no_token(self, client):
        assert client.get("/api/config").status_code == 200

    def test_lookalike_path_is_not_gated(self, client):
        assert client.get("/api/administrators").status_code == 200

    def test_admin_without_token_is_401(self, client):
        response = client.get("/api/admin/ping")
        assert response.status_code == 401
        assert response.json() == {"detail": "Missing authentication token"}

    def test_admin_401_is_not_a_redirect(self, client):
        """The console fetches this surface with XHR.

        A 302 to an HTML login page surfaces in the browser as an opaque CORS
        failure, not as something the client can act on — which is also why the
        ingress uses an `(auth_api)` snippet with no `handle_response`.
        """
        response = client.get("/api/admin/ping", follow_redirects=False)
        assert response.status_code == 401
        assert "location" not in {k.lower() for k in response.headers}

    def test_bearer_header_passes_the_gate(self, client):
        """The gate only sniffs for a token; validity is the endpoint's business."""
        response = client.get("/api/admin/ping", headers={"Authorization": "Bearer whatever"})
        assert response.status_code == 200

    def test_oauth2_proxy_header_passes_the_gate(self, client):
        response = client.get(
            "/api/admin/ping", headers={"X-Auth-Request-Access-Token": "whatever"}
        )
        assert response.status_code == 200

    def test_non_bearer_authorization_is_rejected(self, client):
        response = client.get("/api/admin/ping", headers={"Authorization": "Basic abc"})
        assert response.status_code == 401

    def test_empty_proxy_header_is_rejected(self, client):
        response = client.get("/api/admin/ping", headers={"X-Auth-Request-Access-Token": ""})
        assert response.status_code == 401
