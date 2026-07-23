from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import celine.onboarding.services.dataspace_identity as di

_OriginalAsyncClient = httpx.AsyncClient


@pytest.fixture(autouse=True)
def _reset_token_provider():
    di._token_provider = None
    yield
    di._token_provider = None


@pytest.fixture()
def _enable_vc(monkeypatch):
    monkeypatch.setattr(di.settings, "dataspace_vc_enabled", True)
    monkeypatch.setattr(di.settings, "identity_registry_url", "http://ir:30005")
    monkeypatch.setattr(di.settings, "oidc_base_url", "http://kc:8080/realms/test")
    monkeypatch.setattr(di.settings, "ds_onboarding_client_id", "svc-ds-onboarding")
    monkeypatch.setattr(di.settings, "ds_onboarding_client_secret", "secret")
    monkeypatch.setattr(di.settings, "dataspace_subject_source", "email_hash")
    monkeypatch.setattr(di.settings, "dataspace_user_role", "DataSubject")
    monkeypatch.setattr(di.settings, "dataspace_vc_ttl_days", 365)
    monkeypatch.setattr(di.settings, "dataspace_allowed_actions", "consent.manage,data.share")
    monkeypatch.setattr(di.settings, "dataspace_linked_participant_did", "did:web:rec.example")


CREDENTIAL_RESPONSE = {
    "subjectDid": "did:web:users.example:email-abc123",
    "credentialId": "urn:uuid:cred-001",
    "generatedAt": "2026-07-13T10:00:00Z",
}


def _mock_token_provider():
    token = MagicMock()
    token.access_token = "test-token"
    token.is_valid.return_value = True
    provider = AsyncMock()
    provider.get_token.return_value = token
    return provider


def _patch_httpx(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def factory(**kw):
        kw.pop("transport", None)
        return _OriginalAsyncClient(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


# ── skip when disabled ────────────────────────────────────────────


async def test_skip_when_vc_disabled(monkeypatch, submission):
    monkeypatch.setattr(di.settings, "dataspace_vc_enabled", False)
    await di.provision_user_identity(submission)
    assert submission.dataspace_did is None


async def test_skip_when_already_issued(monkeypatch, submission, _enable_vc):
    submission.dataspace_vc_id = "existing"
    await di.provision_user_identity(submission)
    assert submission.dataspace_did is None


# ── successful credential issuance ────────────────────────────────


async def test_issues_credential_via_http(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    _patch_httpx(monkeypatch, lambda req: httpx.Response(201, json=CREDENTIAL_RESPONSE))

    await di.provision_user_identity(submission)

    assert submission.dataspace_did == "did:web:users.example:email-abc123"
    assert submission.dataspace_vc_id == "urn:uuid:cred-001"
    assert submission.dataspace_subject_id is not None


async def test_request_body_contents(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = req.content
        captured["auth"] = req.headers.get("authorization")
        return httpx.Response(201, json=CREDENTIAL_RESPONSE)

    _patch_httpx(monkeypatch, handler)
    await di.provision_user_identity(submission)

    assert captured["url"] == "http://ir:30005/admin/credentials/data-subject"
    assert captured["auth"] == "Bearer test-token"

    body = json.loads(captured["body"])
    assert body["role"] == "DataSubject"
    assert body["ttl_days"] == 365
    assert body["linked_participant_did"] == "did:web:rec.example"
    assert body["allowed_actions"] == ["consent.manage", "data.share"]


async def test_generated_at_parsed(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    _patch_httpx(monkeypatch, lambda req: httpx.Response(201, json=CREDENTIAL_RESPONSE))

    await di.provision_user_identity(submission)

    assert submission.dataspace_vc_issued_at == datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)


# ── error handling ────────────────────────────────────────────────


async def test_error_on_http_failure(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    _patch_httpx(monkeypatch, lambda req: httpx.Response(500, text="internal error"))

    with pytest.raises(ValueError, match="Credential issuance failed"):
        await di.provision_user_identity(submission)


async def test_error_on_missing_did_in_response(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    _patch_httpx(
        monkeypatch,
        lambda req: httpx.Response(
            201, json={"credentialId": "x", "generatedAt": "2026-01-01T00:00:00Z"}
        ),
    )

    with pytest.raises(ValueError, match="missing subjectDid"):
        await di.provision_user_identity(submission)


async def test_error_when_registry_url_missing(monkeypatch, submission, _enable_vc):
    monkeypatch.setattr(di.settings, "identity_registry_url", "")
    with pytest.raises(ValueError, match="IDENTITY_REGISTRY_URL is required"):
        await di.provision_user_identity(submission)


# ── M2M auth token ────────────────────────────────────────────────


async def test_uses_oidc_token(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    auth_headers = []

    def handler(req: httpx.Request) -> httpx.Response:
        auth_headers.append(req.headers.get("authorization"))
        return httpx.Response(201, json=CREDENTIAL_RESPONSE)

    _patch_httpx(monkeypatch, handler)
    await di.provision_user_identity(submission)

    assert auth_headers[0] == "Bearer test-token"
    di._token_provider.get_token.assert_awaited_once()


# ── organization membership ───────────────────────────────────────


@pytest.fixture()
def _enable_membership(monkeypatch):
    monkeypatch.setattr(di.settings, "dataspace_organization_alias", "rec-example")
    monkeypatch.setattr(di.settings, "dataspace_organization_name", "REC Example")
    monkeypatch.setattr(di.settings, "dataspace_organization_did", "")
    monkeypatch.setattr(di.settings, "dataspace_organization_auto_create", True)
    monkeypatch.setattr(di.settings, "dataspace_membership_role", "member")


def _membership_handler(calls, *, owners_status=201, membership_status=201):
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        calls.append((req.method, url, req.content.decode() if req.content else ""))
        if "credentials/data-subject" in url:
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)
        if "admin/owners" in url:
            return httpx.Response(owners_status, json={"id": "rec-example"})
        if "admin/memberships" in url:
            return httpx.Response(membership_status, json={"user_did": "x"})
        if "keycloak/sync" in url:
            return httpx.Response(200, json={"status": "synced"})
        return httpx.Response(404)

    return handler


async def test_membership_skipped_without_org_alias(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    calls = []
    _patch_httpx(monkeypatch, _membership_handler(calls))

    await di.provision_user_identity(submission)

    assert not any("memberships" in url for _, url, _ in calls)


async def test_membership_registered_after_credential(
    monkeypatch, submission, _enable_vc, _enable_membership
):
    di._token_provider = _mock_token_provider()
    calls = []
    _patch_httpx(monkeypatch, _membership_handler(calls))

    await di.provision_user_identity(submission)

    paths = [url for _, url, _ in calls]
    assert "credentials/data-subject" in paths[0]
    assert "admin/owners" in paths[1]
    assert "admin/memberships" in paths[2]

    body = json.loads(calls[2][2])
    assert body == {
        "user_did": CREDENTIAL_RESPONSE["subjectDid"],
        "organization_alias": "rec-example",
        "role": "member",
    }


async def test_organization_created_with_name_and_did(
    monkeypatch, submission, _enable_vc, _enable_membership
):
    monkeypatch.setattr(di.settings, "dataspace_organization_did", "did:web:rec.example")
    di._token_provider = _mock_token_provider()
    calls = []
    _patch_httpx(monkeypatch, _membership_handler(calls))

    await di.provision_user_identity(submission)

    owner_body = json.loads(next(c[2] for c in calls if "admin/owners" in c[1]))
    assert owner_body == {
        "id": "rec-example",
        "name": "REC Example",
        "did": "did:web:rec.example",
    }


async def test_organization_conflict_is_success(
    monkeypatch, submission, _enable_vc, _enable_membership
):
    di._token_provider = _mock_token_provider()
    calls = []
    _patch_httpx(monkeypatch, _membership_handler(calls, owners_status=409))

    await di.provision_user_identity(submission)

    assert any("admin/memberships" in url for _, url, _ in calls)


async def test_membership_conflict_is_success(
    monkeypatch, submission, _enable_vc, _enable_membership
):
    di._token_provider = _mock_token_provider()
    calls = []
    _patch_httpx(monkeypatch, _membership_handler(calls, membership_status=409))

    await di.provision_user_identity(submission)

    assert submission.dataspace_did == CREDENTIAL_RESPONSE["subjectDid"]


async def test_membership_failure_raises(
    monkeypatch, submission, _enable_vc, _enable_membership
):
    di._token_provider = _mock_token_provider()
    calls = []
    _patch_httpx(monkeypatch, _membership_handler(calls, membership_status=500))

    with pytest.raises(ValueError, match="Membership registration failed"):
        await di.provision_user_identity(submission)


async def test_organization_creation_skipped_when_disabled(
    monkeypatch, submission, _enable_vc, _enable_membership
):
    monkeypatch.setattr(di.settings, "dataspace_organization_auto_create", False)
    di._token_provider = _mock_token_provider()
    calls = []
    _patch_httpx(monkeypatch, _membership_handler(calls))

    await di.provision_user_identity(submission)

    assert not any("admin/owners" in url for _, url, _ in calls)
    assert any("admin/memberships" in url for _, url, _ in calls)


async def test_invalid_org_alias_rejected(
    monkeypatch, submission, _enable_vc, _enable_membership
):
    monkeypatch.setattr(di.settings, "dataspace_organization_alias", "REC_Example!")
    di._token_provider = _mock_token_provider()
    _patch_httpx(monkeypatch, _membership_handler([]))

    with pytest.raises(ValueError, match="DATASPACE_ORGANIZATION_ALIAS"):
        await di.provision_user_identity(submission)


async def test_membership_deleted_on_kc_sync_failure(
    monkeypatch, submission, _enable_vc, _enable_membership
):
    di._token_provider = _mock_token_provider()
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        calls.append((req.method, url))
        if "credentials/data-subject" in url and req.method == "POST":
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)
        if "admin/owners" in url:
            return httpx.Response(409)
        if "admin/memberships" in url and req.method == "POST":
            return httpx.Response(201, json={})
        if "keycloak/sync" in url:
            return httpx.Response(500, text="KC unavailable")
        if req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    _patch_httpx(monkeypatch, handler)

    with pytest.raises(ValueError, match="credential .* has been revoked"):
        await di.provision_user_identity(
            submission,
            keycloak_user_id="kc-user-123",
            keycloak_realm="dataspaces",
        )

    deletes = [url for method, url in calls if method == "DELETE"]
    assert any("memberships" in u and "rec-example" in u for u in deletes)
    assert any("credentials/urn:uuid:cred-001" in u for u in deletes)


# ── KC sync ───────────────────────────────────────────────────────


async def test_kc_sync_called_after_credential(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if "credentials/data-subject" in str(req.url):
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)
        return httpx.Response(200, json={"status": "synced"})

    _patch_httpx(monkeypatch, handler)
    await di.provision_user_identity(
        submission,
        keycloak_user_id="kc-user-123",
        keycloak_realm="dataspaces",
    )

    assert len(calls) == 2
    assert "credentials/data-subject" in calls[0]
    assert "keycloak/sync" in calls[1]


async def test_kc_sync_skipped_without_user_id(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(201, json=CREDENTIAL_RESPONSE)

    _patch_httpx(monkeypatch, handler)
    await di.provision_user_identity(submission)

    assert len(calls) == 1
    assert "keycloak/sync" not in calls[0]


# ── KC sync rollback ─────────────────────────────────────────────


async def test_rollback_on_kc_sync_failure(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        calls.append((req.method, url))
        if "credentials/data-subject" in url and req.method == "POST":
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)
        if "keycloak/sync" in url:
            return httpx.Response(500, text="KC unavailable")
        if req.method == "DELETE" and "credentials/" in url:
            return httpx.Response(204)
        return httpx.Response(404)

    _patch_httpx(monkeypatch, handler)

    with pytest.raises(ValueError, match="credential .* has been revoked"):
        await di.provision_user_identity(
            submission,
            keycloak_user_id="kc-user-123",
            keycloak_realm="dataspaces",
        )

    delete_calls = [(m, u) for m, u in calls if m == "DELETE"]
    assert len(delete_calls) == 1
    assert "urn:uuid:cred-001" in delete_calls[0][1]


async def test_kc_sync_retries_before_rollback(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    sync_attempts = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "credentials/data-subject" in url and req.method == "POST":
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)
        if "keycloak/sync" in url:
            sync_attempts.append(1)
            return httpx.Response(500, text="fail")
        if req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    _patch_httpx(monkeypatch, handler)

    with pytest.raises(ValueError, match="3 attempts"):
        await di.provision_user_identity(
            submission,
            keycloak_user_id="kc-user-123",
            keycloak_realm="dataspaces",
        )

    assert len(sync_attempts) == 3


# ── subject ID derivation ────────────────────────────────────────


def test_email_subject_id():
    result = di._email_subject_id("User@Example.COM")
    assert result.startswith("email-")
    assert len(result) == len("email-") + 24


def test_email_subject_id_empty():
    with pytest.raises(ValueError, match="empty"):
        di._email_subject_id("")
