"""The IP recorded as consent evidence and in the audit trail is the connection's peer.

`X-Forwarded-For` and `X-Real-IP` are written by whoever sends the request, so a
service that reads them lets any caller choose the address its consent or its
audit row carries. uvicorn applies the forwarded headers itself, and only from a
peer listed in `FORWARDED_ALLOW_IPS`; this service reads what uvicorn resolved.
The addresses are from the documentation ranges (RFC 5737).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from celine.onboarding.api import deps
from celine.onboarding.api.admin.deps import client_ip
from celine.onboarding.config.settings import settings

PEER = "198.51.100.20"
FORGED = "203.0.113.66"
FORGED_HEADERS = {"X-Forwarded-For": f"{FORGED}, 192.0.2.1", "X-Real-IP": FORGED}


def _request(headers: dict[str, str], peer: str | None = PEER) -> StarletteRequest:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 50000) if peer else None,
    }
    return StarletteRequest(scope)


class TestTheAuditIp:
    def test_a_forged_forwarded_header_is_ignored(self):
        """
        @verifies REQ-0020
        """
        assert client_ip(_request(FORGED_HEADERS)) == PEER

    def test_without_a_peer_it_is_unknown_not_the_header(self):
        """
        @verifies REQ-0020
        """
        assert client_ip(_request(FORGED_HEADERS, peer=None)) == "unknown"


@pytest.fixture()
def consent_capture(seed_rec, monkeypatch):
    """The public create route, with the service stubbed to record the IP it is given."""
    from celine.onboarding.api import submissions as api
    from celine.onboarding.models.database import get_db
    from celine.onboarding.services import submission_service

    seed_rec("rec-a", steps=["consents", "personal", "review"])
    seen: list[str] = []

    async def _create(db, data, ip, rec_slug):
        seen.append(ip)
        raise HTTPException(418, "stop here")

    async def _db():
        yield None

    monkeypatch.setattr(submission_service, "create_from_consent", _create)
    monkeypatch.setattr(settings, "rate_limit_submissions", "1000/minute")
    deps.limiter.reset()

    app = FastAPI()
    app.state.limiter = deps.limiter
    app.include_router(api.router, prefix="/api/{rec_slug}")
    app.dependency_overrides[get_db] = _db
    yield app, seen
    deps.limiter.reset()


CONSENT = {
    "gdpr_consent": True,
    "gdpr_consent_version": "1",
    "policy_consent": True,
    "policy_consent_version": "1",
    "statute_consent": True,
    "statute_consent_version": "1",
}


class TestTheConsentIp:
    def test_a_forged_forwarded_header_is_ignored(self, consent_capture):
        """
        @verifies REQ-0020
        """
        app, seen = consent_capture
        client = TestClient(app, client=(PEER, 50000))
        response = client.post("/api/rec-a/submissions", json=CONSENT, headers=FORGED_HEADERS)

        assert response.status_code == 418
        assert seen == [PEER]

    def test_behind_a_trusted_proxy_uvicorn_supplies_the_client(self, consent_capture):
        """What `FORWARDED_ALLOW_IPS` does: a peer it lists has its forwarded
        client applied by uvicorn, and the service records that one.

        @verifies REQ-0020
        """
        app, seen = consent_capture
        proxied = ProxyHeadersMiddleware(app, trusted_hosts=[PEER])
        client = TestClient(proxied, client=(PEER, 50000))
        client.post(
            "/api/rec-a/submissions", json=CONSENT, headers={"X-Forwarded-For": "192.0.2.44"}
        )

        assert seen == ["192.0.2.44"]

    def test_from_an_untrusted_peer_uvicorn_ignores_the_header(self, consent_capture):
        """
        @verifies REQ-0020
        """
        app, seen = consent_capture
        proxied = ProxyHeadersMiddleware(app, trusted_hosts=["192.0.2.250"])
        client = TestClient(proxied, client=(PEER, 50000))
        client.post("/api/rec-a/submissions", json=CONSENT, headers=FORGED_HEADERS)

        assert seen == [PEER]


def test_the_limiter_keys_on_the_same_peer():
    """
    @verifies REQ-0020
    """
    request = _request(FORGED_HEADERS)
    assert deps.limiter._key_func(request) == deps.peer_ip(request) == PEER


def test_no_route_reads_a_forwarded_header():
    """A grep, so a later edit that reads the header again fails here.

    @verifies REQ-0020
    """
    from pathlib import Path

    src = Path(__file__).parents[1] / "src" / "celine" / "onboarding"
    offenders = [
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        for line in path.read_text().splitlines()
        if ("x-forwarded-for" in line.lower() or "x-real-ip" in line.lower()) and "headers" in line
    ]
    assert offenders == []
