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
def _enable_vc(monkeypatch, bind_rec):
    # The dataspace binding is per-REC and lives in the manifest, so every test
    # that provisions an identity needs its community bound.
    bind_rec(
        "default",
        organization="rec-example",
        linked_participant_did="did:web:rec.example",
    )
    monkeypatch.setattr(di.settings, "dataspace_enabled", True)
    monkeypatch.setattr(di.settings, "identity_registry_url", "http://ir:30005")
    monkeypatch.setattr(di.settings, "oidc_base_url", "http://kc:8080/realms/test")
    monkeypatch.setattr(di.settings, "ds_onboarding_client_id", "svc-ds-onboarding")
    monkeypatch.setattr(di.settings, "ds_onboarding_client_secret", "secret")
    monkeypatch.setattr(di.settings, "dataspace_subject_source", "email_hash")
    monkeypatch.setattr(di.settings, "dataspace_user_role", "DataSubject")
    monkeypatch.setattr(di.settings, "dataspace_vc_ttl_days", 365)
    monkeypatch.setattr(di.settings, "dataspace_allowed_actions", "consent.manage,data.share")


CREDENTIAL_RESPONSE = {
    "subjectDid": "did:web:users.example:email-abc123",
    "credentialId": "urn:uuid:cred-001",
    "generatedAt": "2026-07-13T10:00:00Z",
}

DERIVE_RESPONSE = {"subject_id": "email-derived123456789012"}


def _default_handler(req: httpx.Request) -> httpx.Response:
    if "users/resolve" in str(req.url):
        return httpx.Response(200, json=DERIVE_RESPONSE)
    return httpx.Response(201, json=CREDENTIAL_RESPONSE)


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


async def test_skip_when_dataspace_disabled(monkeypatch, submission):
    monkeypatch.setattr(di.settings, "dataspace_enabled", False)
    await di.provision_user_identity(submission)
    assert submission.dataspace_did is None


async def test_skip_when_the_rec_has_no_binding(monkeypatch, submission, bind_rec):
    """Both gates must be open: the deployment *and* this community.

    Issuing a credential for a REC that is not in the dataspace would hand
    somebody an identity belonging to no organisation — one the consent
    endpoints refuse to act on, since they gate on membership.
    """
    bind_rec("default")  # no dataspace block
    monkeypatch.setattr(di.settings, "dataspace_enabled", True)
    monkeypatch.setattr(di.settings, "identity_registry_url", "http://ir:30005")

    def handler(req):
        raise AssertionError(f"should not have called {req.url}")

    _patch_httpx(monkeypatch, handler)

    await di.provision_user_identity(submission)
    assert submission.dataspace_did is None


async def test_skip_when_already_issued(monkeypatch, submission, _enable_vc):
    submission.dataspace_vc_id = "existing"
    await di.provision_user_identity(submission)
    assert submission.dataspace_did is None


# ── successful credential issuance ────────────────────────────────


async def test_issues_credential_via_http(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    _patch_httpx(monkeypatch, _default_handler)

    await di.provision_user_identity(submission)

    assert submission.dataspace_did == "did:web:users.example:email-abc123"
    assert submission.dataspace_vc_id == "urn:uuid:cred-001"
    assert submission.dataspace_subject_id is not None


async def test_request_body_contents(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if "users/resolve" in str(req.url):
            return httpx.Response(200, json=DERIVE_RESPONSE)
        if "credentials/data-subject" in str(req.url):
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
    _patch_httpx(monkeypatch, _default_handler)

    await di.provision_user_identity(submission)

    assert submission.dataspace_vc_issued_at == datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)


# ── error handling ────────────────────────────────────────────────


async def test_error_on_http_failure(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()

    def handler(req: httpx.Request) -> httpx.Response:
        if "users/resolve" in str(req.url):
            return httpx.Response(200, json=DERIVE_RESPONSE)
        return httpx.Response(500, text="internal error")

    _patch_httpx(monkeypatch, handler)

    with pytest.raises(ValueError, match="Credential issuance failed"):
        await di.provision_user_identity(submission)


async def test_error_on_missing_did_in_response(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()

    def handler(req: httpx.Request) -> httpx.Response:
        if "users/resolve" in str(req.url):
            return httpx.Response(200, json=DERIVE_RESPONSE)
        return httpx.Response(
            201, json={"credentialId": "x", "generatedAt": "2026-01-01T00:00:00Z"}
        )

    _patch_httpx(monkeypatch, handler)

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
        if "users/resolve" in str(req.url):
            return httpx.Response(200, json=DERIVE_RESPONSE)
        return httpx.Response(201, json=CREDENTIAL_RESPONSE)

    _patch_httpx(monkeypatch, handler)
    await di.provision_user_identity(submission)

    assert auth_headers[0] == "Bearer test-token"
    di._token_provider.get_token.assert_awaited_once()


# ── organization membership ───────────────────────────────────────


def _membership_handler(calls, *, membership_status=201):
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        calls.append((req.method, url, req.content.decode() if req.content else ""))
        if "users/resolve" in url:
            return httpx.Response(200, json=DERIVE_RESPONSE)
        if "credentials/data-subject" in url:
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)
        if "admin/memberships" in url:
            return httpx.Response(membership_status, json={"user_did": "x"})
        if "keycloak/sync" in url:
            return httpx.Response(200, json={"status": "synced"})
        return httpx.Response(404)

    return handler


async def test_membership_registered_after_credential(
    monkeypatch, submission, _enable_vc
):
    di._token_provider = _mock_token_provider()
    calls = []
    _patch_httpx(monkeypatch, _membership_handler(calls))

    await di.provision_user_identity(submission)

    paths = [url for _, url, _ in calls]
    assert "users/resolve" in paths[0]
    assert "credentials/data-subject" in paths[1]
    assert "admin/memberships" in paths[2]

    body = json.loads(calls[2][2])
    assert body == {
        "user_did": CREDENTIAL_RESPONSE["subjectDid"],
        "organization_alias": "rec-example",
        "role": "member",
    }


async def test_organization_is_never_created(
    monkeypatch, submission, _enable_vc
):
    """Onboarding must not mint dataspace trust state.

    An owner created from an approval carries no verification, no agreement and
    therefore no declared capacity — and capacity is what the connector's circle
    check reads. The organisation is seeded by an operator from the deployment's
    owners.yaml through the registry's gated chain, never from here.
    """
    di._token_provider = _mock_token_provider()
    calls = []
    _patch_httpx(monkeypatch, _membership_handler(calls))

    await di.provision_user_identity(submission)

    assert not any("admin/owners" in url for _, url, _ in calls)


async def test_missing_organization_is_an_actionable_error(
    monkeypatch, submission, _enable_vc
):
    """A 404 on membership means the org was never seeded — say so."""
    di._token_provider = _mock_token_provider()
    calls = []
    _patch_httpx(monkeypatch, _membership_handler(calls, membership_status=404))

    with pytest.raises(ValueError, match="does not exist in the identity registry"):
        await di.provision_user_identity(submission)


async def test_membership_conflict_is_success(
    monkeypatch, submission, _enable_vc
):
    di._token_provider = _mock_token_provider()
    calls = []
    _patch_httpx(monkeypatch, _membership_handler(calls, membership_status=409))

    await di.provision_user_identity(submission)

    assert submission.dataspace_did == CREDENTIAL_RESPONSE["subjectDid"]


async def test_membership_failure_raises(
    monkeypatch, submission, _enable_vc
):
    di._token_provider = _mock_token_provider()
    calls = []
    _patch_httpx(monkeypatch, _membership_handler(calls, membership_status=500))

    with pytest.raises(ValueError, match="Membership registration failed"):
        await di.provision_user_identity(submission)


async def test_binding_is_per_rec(monkeypatch, submission, _enable_vc, bind_rec):
    """Two communities in one deployment must not share an organisation.

    This is the defect the manifest binding exists to fix: the binding used to be
    a global environment variable, so every approved member landed in the same
    dataspace organisation — silently, since the wrong membership is still a 201.
    """
    bind_rec("rec-a", organization="org-a")
    bind_rec("rec-b", organization="org-b")
    di._token_provider = _mock_token_provider()

    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "users/resolve" in url:
            return httpx.Response(200, json=DERIVE_RESPONSE)
        if "credentials/data-subject" in url:
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)
        if "admin/memberships" in url:
            seen.append(json.loads(req.content.decode())["organization_alias"])
            return httpx.Response(201, json={"user_did": "x"})
        return httpx.Response(404)

    _patch_httpx(monkeypatch, handler)

    for slug in ("rec-a", "rec-b"):
        submission.rec_slug = slug
        submission.dataspace_vc_id = None
        await di.provision_user_identity(submission)

    assert seen == ["org-a", "org-b"]


async def test_membership_deleted_on_kc_sync_failure(
    monkeypatch, submission, _enable_vc
):
    di._token_provider = _mock_token_provider()
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        calls.append((req.method, url))
        if "users/resolve" in url:
            return httpx.Response(200, json=DERIVE_RESPONSE)
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
        url = str(req.url)
        calls.append(url)
        if "users/resolve" in url:
            return httpx.Response(200, json=DERIVE_RESPONSE)
        if "credentials/data-subject" in url:
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)
        return httpx.Response(200, json={"status": "synced"})

    _patch_httpx(monkeypatch, handler)
    await di.provision_user_identity(
        submission,
        keycloak_user_id="kc-user-123",
        keycloak_realm="dataspaces",
    )

    assert len(calls) == 4
    assert "users/resolve" in calls[0]
    assert "credentials/data-subject" in calls[1]
    assert "admin/memberships" in calls[2]
    assert "keycloak/sync" in calls[3]


async def test_kc_sync_partial_is_accepted_and_warned(
    monkeypatch, submission, _enable_vc, caplog
):
    """A 200 'partial' sync must succeed (no rollback) but log a warning."""
    di._token_provider = _mock_token_provider()
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        calls.append((req.method, url))
        if "users/resolve" in url:
            return httpx.Response(200, json=DERIVE_RESPONSE)
        if "credentials/data-subject" in url:
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)
        if "admin/memberships" in url:
            return httpx.Response(201, json={"user_did": "x"})
        if "keycloak/sync" in url:
            return httpx.Response(
                200,
                json={
                    "status": "partial",
                    "did": CREDENTIAL_RESPONSE["subjectDid"],
                    "keycloak_attribute_synced": False,
                    "warning": "attribute push failed",
                },
            )
        return httpx.Response(404)

    _patch_httpx(monkeypatch, handler)

    import logging

    with caplog.at_level(logging.WARNING):
        await di.provision_user_identity(
            submission,
            keycloak_user_id="kc-user-123",
            keycloak_realm="dataspaces",
        )

    # succeeded: credential kept, no DELETE issued
    assert not any(m == "DELETE" for m, _ in calls)
    assert submission.dataspace_vc_id == CREDENTIAL_RESPONSE["credentialId"]
    assert any("partial" in r.message for r in caplog.records)


async def test_kc_sync_skipped_without_user_id(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        calls.append(url)
        if "users/resolve" in url:
            return httpx.Response(200, json=DERIVE_RESPONSE)
        return httpx.Response(201, json=CREDENTIAL_RESPONSE)

    _patch_httpx(monkeypatch, handler)
    await di.provision_user_identity(submission)

    assert not any("keycloak/sync" in url for url in calls)


# ── KC sync rollback ─────────────────────────────────────────────


async def test_rollback_on_kc_sync_failure(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        calls.append((req.method, url))
        if "users/resolve" in url:
            return httpx.Response(200, json=DERIVE_RESPONSE)
        if "credentials/data-subject" in url and req.method == "POST":
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)
        if "admin/memberships" in url and req.method == "POST":
            return httpx.Response(201, json={"user_did": "x"})
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

    # The rollback unwinds both: membership first, then the credential. Leaving
    # either behind would be trust state with no Keycloak mapping behind it.
    delete_calls = [u for m, u in calls if m == "DELETE"]
    assert len(delete_calls) == 2
    assert "admin/memberships" in delete_calls[0]
    assert "urn:uuid:cred-001" in delete_calls[1]


async def test_kc_sync_retries_before_rollback(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    sync_attempts = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "users/resolve" in url:
            return httpx.Response(200, json=DERIVE_RESPONSE)
        if "credentials/data-subject" in url and req.method == "POST":
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)
        if "admin/memberships" in url and req.method == "POST":
            return httpx.Response(201, json={"user_did": "x"})
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


# ── subject ID derivation (IR-delegated) ─────────────────────────


RESOLVE_EXISTING_RESPONSE = {
    "did": "did:web:users.example:email-abc123",
    "subject_id": "email-oldsha256hash12345678",
    "roles": ["DataSubject"],
    "credentials": [],
}


async def test_uses_ir_derived_subject_id(monkeypatch, submission, _enable_vc):
    """The IR is the sole authority on email→subject_id derivation."""
    di._token_provider = _mock_token_provider()
    captured_body = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if "users/resolve" in str(req.url):
            assert "derive=true" in str(req.url)
            return httpx.Response(200, json=DERIVE_RESPONSE)
        if "credentials/data-subject" in str(req.url):
            captured_body.update(json.loads(req.content))
        return httpx.Response(201, json=CREDENTIAL_RESPONSE)

    _patch_httpx(monkeypatch, handler)
    await di.provision_user_identity(submission)

    assert captured_body["subject_id"] == DERIVE_RESPONSE["subject_id"]


async def test_reuses_existing_subject_id(monkeypatch, submission, _enable_vc):
    """When the IR already has a mapping, the existing subject_id is returned."""
    di._token_provider = _mock_token_provider()
    captured_body = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if "users/resolve" in str(req.url):
            return httpx.Response(200, json=RESOLVE_EXISTING_RESPONSE)
        if "credentials/data-subject" in str(req.url):
            captured_body.update(json.loads(req.content))
        return httpx.Response(201, json=CREDENTIAL_RESPONSE)

    _patch_httpx(monkeypatch, handler)
    await di.provision_user_identity(submission)

    assert captured_body["subject_id"] == "email-oldsha256hash12345678"


async def test_resolve_failure_is_fatal(monkeypatch, submission, _enable_vc):
    """The IR owns derivation — if it is unreachable, provisioning fails."""
    di._token_provider = _mock_token_provider()

    def handler(req: httpx.Request) -> httpx.Response:
        if "users/resolve" in str(req.url):
            return httpx.Response(500, text="internal error")
        return httpx.Response(201, json=CREDENTIAL_RESPONSE)

    _patch_httpx(monkeypatch, handler)

    with pytest.raises(ValueError, match="Subject id derivation failed"):
        await di.provision_user_identity(submission)
