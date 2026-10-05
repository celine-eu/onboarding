"""`/users/resolve` is a POST with a JSON body, the only form ds serves.

An email in a query string is recorded by every access log, proxy and trace on
the path, so ds resolves a person only by ``POST /users/resolve`` and answers a
GET with 405. This service asks with the POST and nothing else.

A registry without the POST (a 405, or the router's own 404) is an error in
every posture, never retried in another form. The registry's *mapping* 404 is
"no such person".
"""

from __future__ import annotations

import json
import logging
import uuid

import httpx
import pytest

import celine.onboarding.services.dataspace_identity as di

_OriginalAsyncClient = httpx.AsyncClient

EMAIL = "member@example.org"
FOUND = {"subject_id": "sub-1", "did": "did:web:rec.example.org:users:sub-1"}
NO_MAPPING = {"detail": "No mapping found for this user"}


def _patch_httpx(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def factory(**kw):
        kw.pop("transport", None)
        return _OriginalAsyncClient(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _access() -> di.RegistryAccess:
    return di.RegistryAccess(base_url="http://ir:30005", headers={"authorization": "Bearer t"})


# ── the POST ──────────────────────────────────────────────────────────────


async def test_resolve_posts_the_identifiers_in_a_json_body(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=FOUND)

    _patch_httpx(monkeypatch, handler)
    resolved = await di.resolve_subject(
        _access(),
        email=EMAIL,
        keycloak_realm="celine",
        keycloak_user_id="kc-1",
        username="member-1",
    )

    assert resolved.subject_id == "sub-1"
    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/users/resolve"
    assert not req.url.params, "no identifier may travel in the URL"
    assert json.loads(req.content) == {
        "email": EMAIL,
        "realm": "celine",
        "user_id": "kc-1",
        "username": "member-1",
    }
    assert req.headers["authorization"] == "Bearer t"


async def test_the_body_carries_only_what_ds_accepts(monkeypatch):
    """ds forbids extra fields, so ``derive`` (GET-only) is never in the body."""
    bodies: list[dict] = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=FOUND)

    _patch_httpx(monkeypatch, handler)
    await di.resolve_subject(_access(), email=EMAIL)
    await di.resolve_subject_and_credential(_access(), email=EMAIL)

    assert bodies == [{"email": EMAIL}, {"email": EMAIL}]


async def test_the_mapping_404_is_no_person_and_is_not_retried_as_a_get(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(404, json=NO_MAPPING)

    _patch_httpx(monkeypatch, handler)
    resolved = await di.resolve_subject(_access(), email=EMAIL)

    assert uuid.UUID(resolved.subject_id).version == 4
    assert [r.method for r in seen] == ["POST"]


# ── a registry without the POST ──────────────────────────────────────────


@pytest.mark.parametrize("env", ["dev", "prod", ""])
async def test_a_405_is_an_error_logged_without_identifiers(monkeypatch, caplog, env):
    """No posture retries in another form; the log names the status only."""
    monkeypatch.setenv("CELINE_ENV", env)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        if req.method != "POST":
            return httpx.Response(200, json=FOUND)
        return httpx.Response(405, json={"detail": "Method Not Allowed"})

    _patch_httpx(monkeypatch, handler)

    with caplog.at_level(logging.DEBUG, logger=di.logger.name):
        with pytest.raises(ValueError, match="no POST /users/resolve") as raised:
            await di.resolve_subject(
                _access(),
                email=EMAIL,
                username="member-1",
                keycloak_realm="celine",
                keycloak_user_id="kc-1",
            )

    assert [r.method for r in seen] == ["POST"], "nothing is sent after the 405"
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors and "405" in errors[0]
    text = "\n".join(r.getMessage() for r in caplog.records) + str(raised.value)
    for value in (EMAIL, "member-1", "kc-1"):
        assert value not in text


# ── what reaches the log ─────────────────────────────────────────────────


async def test_a_409_logs_the_keycloak_id_and_never_the_email(monkeypatch, caplog):
    _patch_httpx(
        monkeypatch,
        lambda req: httpx.Response(409, json={"detail": "identifier conflict: ..."}),
    )

    with caplog.at_level(logging.ERROR, logger=di.logger.name):
        with pytest.raises(di.SubjectIdentifierConflictError):
            await di.resolve_subject(
                _access(),
                email=EMAIL,
                username="member-1",
                keycloak_realm="celine",
                keycloak_user_id="kc-1",
            )

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "kc-1" in text
    assert "email" in text, "which identifiers were sent is still named"
    assert EMAIL not in text
    assert "member-1" not in text


# ── the funnel sends the Keycloak pair when it has one ───────────────────


async def test_provisioning_resolves_with_the_keycloak_pair(monkeypatch, submission, bind_rec):
    bind_rec(
        "default",
        organization="rec-example",
        linked_participant_did="did:web:rec.example",
    )
    monkeypatch.setattr(di.settings, "dataspace_enabled", True)
    monkeypatch.setattr(di.settings, "identity_registry_url", "http://ir:30005")
    monkeypatch.setattr(di, "_auth_headers", _fixed_headers)
    monkeypatch.setattr(di, "_collector_headers", _collector_headers, raising=False)
    bodies: list[dict] = []

    def handler(req):
        if req.url.path == "/users/resolve":
            bodies.append(json.loads(req.content))
            return httpx.Response(404, json=NO_MAPPING)
        if req.url.path == "/admin/credentials/data-subject":
            return httpx.Response(
                201,
                json={
                    "subjectDid": "did:web:rec.example:users:x",
                    "credentialId": "urn:uuid:cred-1",
                    "generatedAt": "2026-10-05T10:00:00Z",
                },
            )
        return httpx.Response(200, json={})

    _patch_httpx(monkeypatch, handler)
    await di.provision_user_identity(
        submission, keycloak_user_id="kc-1", keycloak_realm="celine", provision_shares=False
    )

    assert bodies[0]["realm"] == "celine"
    assert bodies[0]["user_id"] == "kc-1"
    assert bodies[0]["email"] == submission.email


async def _fixed_headers() -> dict[str, str]:
    return {"Authorization": "Bearer service"}


async def _collector_headers(organization_alias: str, scope: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {scope}"}
