"""`/users/resolve` is a POST with a JSON body; the GET is a dev-only fallback.

An email in a query string is recorded by every access log, proxy and trace on
the path, so ds moved the lookup to ``POST /users/resolve`` and withdraws the GET
(410 after its sunset, outside dev). This service asks with the POST.

The local stack runs this service from source against whichever ds it has, and
an older registry has no POST: it answers 405 (or the router's own 404). Under
``CELINE_ENV=dev`` the GET is tried once, with a warning; anywhere else that is a
refusal, because the fallback would put the identifiers back into a URL. The
registry's *mapping* 404 is "no such person", never a reason to fall back.
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


class _OldRegistry:
    """An identity registry from before ``POST /users/resolve``."""

    def __init__(self, post_status: int = 405, get_answer: httpx.Response | None = None):
        self.post_status = post_status
        self.get_answer = get_answer or httpx.Response(200, json=FOUND)
        self.seen: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.seen.append(req)
        if req.method == "POST":
            detail = "Method Not Allowed" if self.post_status == 405 else "Not Found"
            return httpx.Response(self.post_status, json={"detail": detail})
        return self.get_answer


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


# ── an older registry: dev falls back once, elsewhere refuses ─────────────


@pytest.mark.parametrize("post_status", [405, 404])
async def test_dev_falls_back_to_the_get_once_with_a_warning(monkeypatch, caplog, post_status):
    monkeypatch.setenv("CELINE_ENV", "dev")
    registry = _OldRegistry(post_status)
    _patch_httpx(monkeypatch, registry)

    with caplog.at_level(logging.WARNING, logger=di.logger.name):
        resolved = await di.resolve_subject(
            _access(), email=EMAIL, keycloak_realm="celine", keycloak_user_id="kc-1"
        )

    assert resolved.subject_id == "sub-1"
    assert [r.method for r in registry.seen] == ["POST", "GET"]
    get = registry.seen[1]
    assert get.url.params["email"] == EMAIL
    assert get.url.params["derive"] == "false"
    assert get.url.params["user_id"] == "kc-1"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "POST /users/resolve" in warnings[0].getMessage()
    assert EMAIL not in warnings[0].getMessage()


async def test_the_dev_fallback_still_reads_no_mapping_as_none(monkeypatch):
    monkeypatch.setenv("CELINE_ENV", "dev")
    registry = _OldRegistry(get_answer=httpx.Response(404, json=NO_MAPPING))
    _patch_httpx(monkeypatch, registry)

    resolved = await di.resolve_subject(_access(), email=EMAIL)

    assert resolved.did is None
    assert [r.method for r in registry.seen] == ["POST", "GET"]


@pytest.mark.parametrize("env", ["prod", "staging", ""])
@pytest.mark.parametrize("post_status", [405, 404])
async def test_outside_dev_an_old_registry_is_refused(monkeypatch, env, post_status):
    monkeypatch.setenv("CELINE_ENV", env)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    registry = _OldRegistry(post_status)
    _patch_httpx(monkeypatch, registry)

    with pytest.raises(ValueError, match="no POST /users/resolve"):
        await di.resolve_subject(_access(), email=EMAIL)

    assert [r.method for r in registry.seen] == ["POST"], "the GET must never be sent"


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
