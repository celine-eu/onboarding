from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

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

    assert submission.dataspace_vc_issued_at == datetime(2026, 7, 13, 10, 0, tzinfo=UTC)


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

    # No collector secret under CELINE_ENV=dev: the transition makes every
    # registry call as this service's client, as before the collector client.
    assert auth_headers and set(auth_headers) == {"Bearer test-token"}


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


async def test_membership_registered_after_credential(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    calls = []
    _patch_httpx(monkeypatch, _membership_handler(calls))

    await di.provision_user_identity(submission)

    paths = [url for _, url, _ in calls]
    assert "users/resolve" in paths[0]
    assert "credentials/data-subject" in paths[1]
    # No `owners/resolve` hop. Membership is filed under the name the manifest
    # carries, because that name is now the owner id — one identifier, so there
    # is nothing to canonicalise. See the 2026-09-12 convergence.
    assert "admin/memberships" in paths[2]
    assert not any("owners/resolve" in u for u in paths)

    body = json.loads(calls[2][2])
    # No role. It is a claim on the credential, changed by reissue; the registry
    # dropped the column this used to fill, and a body that still named one would
    # read as though membership recorded what somebody is.
    assert body == {
        "user_did": CREDENTIAL_RESPONSE["subjectDid"],
        "organization_alias": "rec-example",
    }


async def test_organization_is_never_created(monkeypatch, submission, _enable_vc):
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


async def test_missing_organization_is_an_actionable_error(monkeypatch, submission, _enable_vc):
    """A 404 on membership means the org was never seeded — say so."""
    di._token_provider = _mock_token_provider()
    calls = []
    _patch_httpx(monkeypatch, _membership_handler(calls, membership_status=404))

    with pytest.raises(ValueError, match="does not exist in the identity registry"):
        await di.provision_user_identity(submission)


async def test_membership_conflict_is_success(monkeypatch, submission, _enable_vc):
    di._token_provider = _mock_token_provider()
    calls = []
    _patch_httpx(monkeypatch, _membership_handler(calls, membership_status=409))

    await di.provision_user_identity(submission)

    assert submission.dataspace_did == CREDENTIAL_RESPONSE["subjectDid"]


async def test_membership_failure_raises(monkeypatch, submission, _enable_vc):
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


async def test_membership_deleted_on_kc_sync_failure(monkeypatch, submission, _enable_vc):
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


async def test_kc_sync_carries_the_username_the_data_plane_joins_on(
    monkeypatch, submission, _enable_vc
):
    """The identity registry is told what this person is called, not just who.

    A dataspace decision names people by DID and the systems holding their data
    do not. The connector translates one to the other through this registry and
    hands the answer to `dataset-api`, which resolves it against the REC
    registry's `Member.user_id` — the same value provisioning wrote there. Both
    ends therefore have to be given one value from one source, and this call is
    where it crosses.
    """
    di._token_provider = _mock_token_provider()
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "users/resolve" in url:
            return httpx.Response(200, json=DERIVE_RESPONSE)
        if "credentials/data-subject" in url:
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)
        if "keycloak/sync" in url:
            bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"status": "synced"})

    _patch_httpx(monkeypatch, handler)
    await di.provision_user_identity(
        submission,
        keycloak_user_id="kc-user-123",
        keycloak_realm="dataspaces",
        keycloak_username="alice.adopted",
    )

    assert bodies[0]["username"] == "alice.adopted"


async def test_kc_sync_sends_the_username_even_when_it_differs_from_the_email(
    monkeypatch, submission, _enable_vc
):
    """The case the email fallback gets wrong, and the reason to send both.

    `_find_user` adopts a Keycloak user whose username is not their email, and
    the registry member then holds that username. Leaving the registry to fall
    back to the email would have the connector name a subject the data plane
    cannot resolve — the row filter matches nobody, the handler denies, and
    somebody who consented silently receives no rows.
    """
    di._token_provider = _mock_token_provider()
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "users/resolve" in url:
            return httpx.Response(200, json=DERIVE_RESPONSE)
        if "credentials/data-subject" in url:
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)
        if "keycloak/sync" in url:
            bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"status": "synced"})

    _patch_httpx(monkeypatch, handler)
    await di.provision_user_identity(
        submission,
        keycloak_user_id="kc-user-123",
        keycloak_realm="dataspaces",
        keycloak_username="adopted-handle",
    )

    assert bodies[0]["username"] == "adopted-handle"
    assert bodies[0]["email"] == "user@example.com"


async def test_kc_sync_omits_the_username_when_there_is_none(monkeypatch, submission, _enable_vc):
    """A retry of this step alone has no provisioning result to read it from.

    Sending `username: null` would overwrite a good value in the registry with
    nothing; omitting the key leaves whatever is already there and lets the
    email fallback stand, which is right for every user this service created.
    """
    di._token_provider = _mock_token_provider()
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "users/resolve" in url:
            return httpx.Response(200, json=DERIVE_RESPONSE)
        if "credentials/data-subject" in url:
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)
        if "keycloak/sync" in url:
            bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"status": "synced"})

    _patch_httpx(monkeypatch, handler)
    await di.provision_user_identity(
        submission,
        keycloak_user_id="kc-user-123",
        keycloak_realm="dataspaces",
    )

    assert "username" not in bodies[0]


async def test_kc_sync_partial_is_accepted_and_warned(monkeypatch, submission, _enable_vc, caplog):
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
    # A live identity: an active credential of some role. With none the DID is
    # spent (a released member) and is not reused; see the tests below.
    "credentials": [{"role": "DataSubject", "vc_jws": "header.payload.sig"}],
}

#: The mapping of a person a REC released: every credential revoked.
RESOLVE_SPENT_RESPONSE = {
    "did": "did:web:rec-a.example:users:old-id",
    "subject_id": "old-id",
    "roles": [],
    "credentials": [],
}


@pytest.mark.parametrize("recorded", [None, "old-id"])
async def test_a_released_person_gets_a_new_subject_id_never_the_old_one(
    monkeypatch, submission, _enable_vc, recorded
):
    """A new DID per REC (requester, 2026-10-05; ds ADR-0028). The mapping still
    names the spent DID; reusing its id is a 409 in the same REC and would link
    the two DIDs by their suffix in another. A release clears the recorded id; one
    revoked before that (the old id still recorded) is refused all the same."""
    di._token_provider = _mock_token_provider()
    submission.dataspace_subject_id = recorded
    captured_body = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if "users/resolve" in str(req.url):
            return httpx.Response(200, json=RESOLVE_SPENT_RESPONSE)
        if "credentials/data-subject" in str(req.url):
            captured_body.update(json.loads(req.content))
        return httpx.Response(201, json=CREDENTIAL_RESPONSE)

    _patch_httpx(monkeypatch, handler)
    await di.provision_user_identity(submission)

    assert captured_body["subject_id"] != "old-id"
    assert uuid.UUID(captured_body["subject_id"]).version == 4
    assert submission.dataspace_subject_id == captured_body["subject_id"]


async def test_a_retry_after_a_failed_sync_keeps_the_id_it_recorded(
    monkeypatch, submission, _enable_vc
):
    """Issued under a new id, then the login sync failed: the mapping still names
    the spent DID, and the retry must reuse the recorded id, not mint a third."""
    di._token_provider = _mock_token_provider()
    submission.dataspace_subject_id = "new-id-recorded-before-issuing"
    captured_body = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if "users/resolve" in str(req.url):
            return httpx.Response(200, json=RESOLVE_SPENT_RESPONSE)
        if "credentials/data-subject" in str(req.url):
            captured_body.update(json.loads(req.content))
        return httpx.Response(201, json=CREDENTIAL_RESPONSE)

    _patch_httpx(monkeypatch, handler)
    await di.provision_user_identity(submission)

    assert captured_body["subject_id"] == "new-id-recorded-before-issuing"


async def test_a_person_with_no_mapping_is_issued_a_minted_id(monkeypatch, submission, _enable_vc):
    """The IR is asked for a mapping and never to derive one; a 404 is "none"."""
    di._token_provider = _mock_token_provider()
    captured_body = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if "users/resolve" in str(req.url):
            assert req.method == "POST"
            assert "derive" not in json.loads(req.content)
            return httpx.Response(404, json={"detail": "No mapping found for this user"})
        if "credentials/data-subject" in str(req.url):
            captured_body.update(json.loads(req.content))
        return httpx.Response(201, json=CREDENTIAL_RESPONSE)

    _patch_httpx(monkeypatch, handler)
    await di.provision_user_identity(submission)

    assert uuid.UUID(captured_body["subject_id"]).version == 4
    assert submission.dataspace_subject_id == captured_body["subject_id"]


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

    with pytest.raises(ValueError, match="Subject resolution failed"):
        await di.provision_user_identity(submission)


# ── reading the consent plane ─────────────────────────────────────
#
# The two calls the POD export makes before it writes a file: resolve the
# offer's controller to the DID the consent plane is keyed by, then ask who
# currently consents. Both fail closed, and the two failure kinds are kept
# apart deliberately — a configuration an operator can fix raises `ValueError`,
# an unreachable or refusing service raises `RuntimeError`, because only the
# second is worth retrying.


@pytest.fixture()
def _consent_plane(monkeypatch):
    monkeypatch.setattr(di.settings, "identity_registry_url", "http://ir:30005")
    monkeypatch.setattr(di.settings, "ds_connector_url", "http://connector:30006")
    monkeypatch.setattr(di.settings, "oidc_base_url", "http://kc:8080/realms/test")
    monkeypatch.setattr(di.settings, "ds_onboarding_client_id", "svc-ds-onboarding")
    monkeypatch.setattr(di.settings, "ds_onboarding_client_secret", "secret")
    di._token_provider = _mock_token_provider()


OWNER_RESPONSE = {
    "id": "grid-operator",
    "name": "Grid Operator",
    "did": "did:web:grid-operator.dataspaces.localhost",
    "aliases": ["grid"],
    "status": "verified",
}

AUDIENCE_RESPONSE = {
    "offer_id": "household-energy-flexibility",
    "consumer_id": "did:web:grid-operator.dataspaces.localhost",
    "purpose": ["FlexibilityResearch"],
    "controller_role": "operations",
    "datasets": [
        {
            "dataset_id": "datasets.silver.meters_15m",
            "subject_ids": ["did:web:users.example:b", "did:web:users.example:a"],
            "subject_count": 2,
        }
    ],
}


async def test_owner_check_carries_the_did(monkeypatch, _consent_plane):
    """`/owners/resolve` already returns it; the registry is the only mapping."""
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json=OWNER_RESPONSE))

    check = await di.check_organization("grid-operator")

    assert check.found is True
    assert check.status == "verified"
    assert check.did == OWNER_RESPONSE["did"]


async def test_owner_check_carries_the_owners_own_id_when_asked_by_alias(
    monkeypatch, _consent_plane
):
    """The registry answers an alias as it answers an id; the id tells them apart."""
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json=OWNER_RESPONSE))

    check = await di.check_organization("grid")

    assert check.id == "grid-operator"


async def test_resolves_the_controller_to_a_did(monkeypatch, _consent_plane):
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(200, json=OWNER_RESPONSE)

    _patch_httpx(monkeypatch, handler)

    assert await di.resolve_consumer_did("grid-operator") == OWNER_RESPONSE["did"]
    # By alias, through the route that does the id-then-alias fallback itself.
    assert "owners/resolve" in captured["url"]
    assert "alias=grid-operator" in captured["url"]


async def test_an_unknown_controller_is_refused(monkeypatch, _consent_plane):
    _patch_httpx(monkeypatch, lambda req: httpx.Response(404, text="Owner not found"))

    with pytest.raises(ValueError, match="does not know"):
        await di.resolve_consumer_did("grid-operator")


async def test_a_controller_without_a_did_is_refused(monkeypatch, _consent_plane):
    """Registered but not onboarded: the consent plane has no key for it.

    This is the state a fresh deployment is in, and the answer must not be a
    plausible guess — the connector would return an audience for a wrong DID
    and nothing in the response would say so.
    """
    _patch_httpx(
        monkeypatch,
        lambda req: httpx.Response(200, json={**OWNER_RESPONSE, "did": None}),
    )

    with pytest.raises(ValueError, match="no dataspace identifier"):
        await di.resolve_consumer_did("grid-operator")


async def test_an_unreachable_registry_is_not_a_missing_owner(monkeypatch, _consent_plane):
    """Retryable, and told apart from the configuration errors beside it."""
    _patch_httpx(monkeypatch, lambda req: httpx.Response(503, text="unavailable"))

    with pytest.raises(RuntimeError, match="could not be reached"):
        await di.resolve_consumer_did("grid-operator")


#: The community's own connector, as `consent_routes` names it for an offer no
#: other participant holds. Routing itself is `test_pod_list_routing.py`.
OWN_ROUTE = di.ConsentRoute(
    offer_id="household-energy-flexibility",
    connector_url="http://connector:30006",
    collector="example-rec",
)
HOLDER_ROUTE = di.ConsentRoute(
    offer_id="household-energy-flexibility",
    connector_url="http://dso-connector:30007",
    collector="example-rec",
    holder="example-dso",
)


async def test_reads_the_audience_for_an_offer(monkeypatch, _consent_plane):
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(200, json=AUDIENCE_RESPONSE)

    _patch_httpx(monkeypatch, handler)

    audience = await di.get_offer_audience(
        "household-energy-flexibility", "did:web:grid-operator.dataspaces.localhost", [OWN_ROUTE]
    )

    assert [d.dataset_id for d in audience.datasets] == ["datasets.silver.meters_15m"]
    assert audience.subject_ids == frozenset({"did:web:users.example:a", "did:web:users.example:b"})
    assert audience.datasets[0].subject_count == 2
    assert audience.routes == (OWN_ROUTE,)
    assert captured["url"].startswith("http://connector:30006/consent/admin/shares")
    assert "consumer_id=did%3Aweb%3Agrid-operator.dataspaces.localhost" in captured["url"]


async def test_the_audience_is_read_at_the_route_given_not_the_setting(monkeypatch, _consent_plane):
    """`DS_CONNECTOR_URL` is the community's own connector, which is not where
    every offer's data is. The caller names the route; this function asks it."""
    asked: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        asked.append(req.url.host)
        return httpx.Response(200, json=AUDIENCE_RESPONSE)

    _patch_httpx(monkeypatch, handler)
    audience = await di.get_offer_audience(
        "household-energy-flexibility", "did:web:x", [HOLDER_ROUTE]
    )

    assert asked == ["dso-connector"]
    assert audience.datasets[0].held_by == "example-dso"


async def test_the_audience_call_sends_no_purpose(monkeypatch, _consent_plane):
    """The connector stamps purpose and role from the offer, and must.

    A caller that cannot supply a purpose cannot omit one, which is what makes
    the under-specification that answers "nobody" on the connector's internal
    check unreachable from here.
    """
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["query"] = req.url.params
        return httpx.Response(200, json=AUDIENCE_RESPONSE)

    _patch_httpx(monkeypatch, handler)
    await di.get_offer_audience("household-energy-flexibility", "did:web:x", [OWN_ROUTE])

    assert set(captured["query"].keys()) == {"offer_id", "consumer_id"}


async def test_datasets_that_agree_are_one_audience(monkeypatch, _consent_plane):
    """The check is per dataset; one distinct set across them is the offer's audience.

    The connector deliberately does not flatten them, and this does not either:
    it compares them. Identical sets are the ordinary case, and one list is then
    exactly true of the whole offer.
    """
    body = {
        **AUDIENCE_RESPONSE,
        "datasets": [
            AUDIENCE_RESPONSE["datasets"][0],
            {**AUDIENCE_RESPONSE["datasets"][0], "dataset_id": "datasets.silver.meters_1h"},
        ],
    }
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json=body))

    audience = await di.get_offer_audience("household-energy-flexibility", "did:web:x", [OWN_ROUTE])

    assert [d.dataset_id for d in audience.datasets] == [
        "datasets.silver.meters_15m",
        "datasets.silver.meters_1h",
    ]
    assert audience.subject_ids == frozenset({"did:web:users.example:a", "did:web:users.example:b"})


async def test_datasets_that_disagree_are_refused_rather_than_merged(monkeypatch, _consent_plane):
    """Neither union nor intersection: the offer's statement is not true of both.

    Union would list someone who withdrew from one dataset; intersection would
    empty silently when a dataset nobody was asked about is bound.
    """
    body = {
        **AUDIENCE_RESPONSE,
        "datasets": [
            AUDIENCE_RESPONSE["datasets"][0],
            {"dataset_id": "datasets.silver.meters_1h", "subject_ids": [], "subject_count": 0},
        ],
    }
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json=body))

    with pytest.raises(di.AudienceSplitError, match="do not agree") as refused:
        await di.get_offer_audience("household-energy-flexibility", "did:web:x", [OWN_ROUTE])

    assert "2 subjects: datasets.silver.meters_15m" in str(refused.value)
    assert "0 subjects: datasets.silver.meters_1h" in str(refused.value)
    assert "2 subjects are in some of these audiences and not in others" in str(refused.value)
    # A caller catching ValueError — the export's API maps it to a 422 — still does.
    assert isinstance(refused.value, ValueError)


async def test_a_contract_offer_is_a_caller_error(monkeypatch, _consent_plane):
    """Disclosed, not consented: a property of the offer, wherever it is asked."""
    _patch_httpx(monkeypatch, lambda req: httpx.Response(409, text="not consent-based"))

    with pytest.raises(ValueError, match="not consent-based"):
        await di.get_offer_audience("household-energy-flexibility", "did:web:x", [OWN_ROUTE])


async def test_a_route_holding_nothing_is_not_the_offer_being_wrong(monkeypatch, _consent_plane):
    """A 422 is that connector holding no dataset for the offer. With no other
    route holding one there is nothing to export — refused, pointing at the
    routing rather than at the caller for naming the offer."""
    _patch_httpx(monkeypatch, lambda req: httpx.Response(422, text="resolves to no dataset"))

    with pytest.raises(ValueError, match="holds a dataset") as refused:
        await di.get_offer_audience("household-energy-flexibility", "did:web:x", [OWN_ROUTE])

    assert "dataspace.connectors" in str(refused.value)
    assert "caller" not in str(refused.value)


async def test_no_route_is_refused_without_naming_a_setting(monkeypatch, _consent_plane):
    with pytest.raises(RuntimeError) as refused:
        await di.get_offer_audience("household-energy-flexibility", "did:web:x", [])
    assert "DS_CONNECTOR_URL" not in str(refused.value)


async def test_an_unreachable_connector_stops_the_export(monkeypatch, _consent_plane):
    """Who consents is unknown, which is not the same as nobody consenting."""
    _patch_httpx(monkeypatch, lambda req: httpx.Response(500, text="boom"))

    with pytest.raises(RuntimeError, match="must not proceed"):
        await di.get_offer_audience("household-energy-flexibility", "did:web:x", [OWN_ROUTE])


async def test_one_unreachable_holder_stops_the_export_even_when_another_answered(
    monkeypatch, _consent_plane
):
    """A holder that cannot be read is not a holder that holds nothing: its
    audience is unknown, so the agreement cannot be checked."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "dso-connector":
            return httpx.Response(503, text="down")
        return httpx.Response(200, json=AUDIENCE_RESPONSE)

    _patch_httpx(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="example-dso's connector answered 503"):
        await di.get_offer_audience(
            "household-energy-flexibility", "did:web:x", [OWN_ROUTE, HOLDER_ROUTE]
        )


async def test_an_empty_dataset_list_is_not_read_as_nobody(monkeypatch, _consent_plane):
    """The connector refuses a dataset-less offer with a 422, so this is a shape
    this caller was not written against — not a true "nobody consents"."""
    _patch_httpx(
        monkeypatch, lambda req: httpx.Response(200, json={**AUDIENCE_RESPONSE, "datasets": []})
    )

    with pytest.raises(RuntimeError, match="no dataset"):
        await di.get_offer_audience("household-energy-flexibility", "did:web:x", [OWN_ROUTE])


# ── Phase 1: provisioning is a function ───────────────────────────
#
# `the-wizard-is-where-a-member-becomes-a-subject`. The wizard's door does not
# exist yet; what these cover is the surface it will call, and the one thing the
# lift changed about the funnel — the credential now records how the person was
# checked.


def _access() -> di.RegistryAccess:
    return di.RegistryAccess(base_url="http://ir:30005", headers={"authorization": "Bearer t"})


class TestResolveSubject:
    async def test_a_new_person_gets_a_minted_id_and_no_did(self, monkeypatch):
        """The ordinary first-time answer. The registry's 404 is not an error."""
        _patch_httpx(monkeypatch, lambda req: httpx.Response(404))

        resolved = await di.resolve_subject(_access(), email="a@example.org")

        assert uuid.UUID(resolved.subject_id).version == 4
        assert resolved.did is None

    async def test_an_id_already_recorded_is_reused_when_nothing_is_mapped(self, monkeypatch):
        _patch_httpx(monkeypatch, lambda req: httpx.Response(404))

        resolved = await di.resolve_subject(
            _access(), email="a@example.org", recorded="0b9c3a2e-6f1d-4e8a-9c7b-2d5e1f0a3b4c"
        )

        assert resolved.subject_id == "0b9c3a2e-6f1d-4e8a-9c7b-2d5e1f0a3b4c"

    async def test_a_mapping_wins_over_an_id_already_recorded(self, monkeypatch):
        """The registry holds the person's one DID; a local copy never overrides it."""
        _patch_httpx(
            monkeypatch,
            lambda req: httpx.Response(
                200, json={"subject_id": "sub-1", "did": "did:web:users.example:users:sub-1"}
            ),
        )

        resolved = await di.resolve_subject(_access(), email="a@example.org", recorded="other")

        assert resolved.subject_id == "sub-1"

    async def test_a_known_person_comes_back_with_their_did(self, monkeypatch):
        """The DID is what lets the caller ask whether to issue at all."""
        _patch_httpx(
            monkeypatch,
            lambda req: httpx.Response(
                200, json={"subject_id": "sub-1", "did": "did:web:users.example:sub-1"}
            ),
        )

        resolved = await di.resolve_subject(_access(), email="a@example.org")

        assert resolved.did == "did:web:users.example:sub-1"

    async def test_derive_is_never_requested_and_the_email_is_sent(self, monkeypatch):
        """A derived id is the registry's HMAC of the email; this setup mints UUIDs.

        The email goes in the POST body, never the URL (R21), and ``derive`` is
        not sent at all: the POST has no such field and ds forbids extras.
        """
        seen: list[httpx.Request] = []

        def handler(req):
            seen.append(req)
            return httpx.Response(200, json=DERIVE_RESPONSE)

        _patch_httpx(monkeypatch, handler)
        await di.resolve_subject(_access(), email="a@example.org")

        assert seen[0].method == "POST"
        assert not seen[0].url.params
        assert json.loads(seen[0].content) == {"email": "a@example.org"}

    async def test_the_keycloak_pair_is_sent_together_or_not_at_all(self, monkeypatch):
        """`realm` alone identifies nobody; the registry wants both or neither."""
        seen: list[dict] = []

        def handler(req):
            seen.append(json.loads(req.content))
            return httpx.Response(200, json=DERIVE_RESPONSE)

        _patch_httpx(monkeypatch, handler)
        await di.resolve_subject(_access(), email="a@example.org", keycloak_realm="celine")

        assert "realm" not in seen[0]

        await di.resolve_subject(
            _access(), email="a@example.org", keycloak_realm="celine", keycloak_user_id="kc-1"
        )
        assert seen[1]["realm"] == "celine"
        assert seen[1]["user_id"] == "kc-1"

    async def test_a_409_is_its_own_error_and_is_logged_for_an_operator(self, monkeypatch, caplog):
        """A member cannot act on this and must not be told to retry.

        ds quarantines the conflict rather than reconciling it, so the only thing
        that moves it forward is an operator noticing — which is what the error
        log is for.
        """
        _patch_httpx(monkeypatch, lambda req: httpx.Response(409, text="identifier email: ..."))

        with caplog.at_level("ERROR"):
            with pytest.raises(di.SubjectIdentifierConflictError):
                await di.resolve_subject(_access(), email="a@example.org")

        assert any(r.levelname == "ERROR" for r in caplog.records)

    async def test_a_conflict_is_not_an_unavailable_registry(self, monkeypatch):
        """The distinction this type exists to make: 409 and 503 are different answers."""
        _patch_httpx(monkeypatch, lambda req: httpx.Response(503))

        with pytest.raises(ValueError) as exc:
            await di.resolve_subject(_access(), email="a@example.org")

        assert not isinstance(exc.value, di.SubjectIdentifierConflictError)

    async def test_a_200_with_no_subject_id_is_a_failure(self, monkeypatch):
        _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json={}))

        with pytest.raises(ValueError):
            await di.resolve_subject(_access(), email="a@example.org")


class TestTheCredentialSaysHowThePersonWasChecked:
    async def test_the_funnel_records_the_rec_and_the_review(
        self, monkeypatch, submission, _enable_vc, bind_rec
    ):
        """`verified_by` / `verification_method`, which this service never sent.

        ds does no KYC by design and defaults both to ``None`` *"because callers
        written before it existed do not send it"* — this caller. A credential
        that says nothing implies no assurance level, which was safe and useless.
        """
        bind_rec(
            "default",
            organization="rec-example",
            organization_did="did:web:rec.example",
            linked_participant_did="did:web:rec.example",
        )
        di._token_provider = _mock_token_provider()
        bodies: list[dict] = []

        def handler(req):
            if "users/resolve" in str(req.url):
                return httpx.Response(200, json=DERIVE_RESPONSE)
            bodies.append(json.loads(req.content))
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)

        _patch_httpx(monkeypatch, handler)
        await di.provision_user_identity(submission)

        assert bodies[0]["verified_by"] == "did:web:rec.example"
        assert bodies[0]["verification_method"] == "submission-review"

    async def test_the_bare_value_is_still_submission_review(self):
        """The prefix every value keeps, and the whole value where nothing was recorded.

        Until 2026-09-14 both doors sent exactly this. The requester then asked for
        the credential to say how the REC checked; the approval door appends the
        recorded method, and a reader matching on the prefix still recognises it.
        """
        assert di.VERIFICATION_METHOD == "submission-review"

    @pytest.mark.parametrize(
        ("method", "expected"),
        [
            ("offline", "submission-review:offline"),
            ("uploaded-document", "submission-review:uploaded-document"),
        ],
    )
    async def test_the_funnel_sends_the_recorded_method(
        self, monkeypatch, submission, _enable_vc, bind_rec, method, expected
    ):
        from types import SimpleNamespace

        from celine.onboarding.models.verification import VerificationMethod

        submission.verification = SimpleNamespace(verification_method=VerificationMethod(method))
        di._token_provider = _mock_token_provider()
        bodies: list[dict] = []

        def handler(req):
            if "users/resolve" in str(req.url):
                return httpx.Response(200, json=DERIVE_RESPONSE)
            bodies.append(json.loads(req.content))
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)

        _patch_httpx(monkeypatch, handler)
        await di.provision_user_identity(submission)

        assert bodies[0]["verification_method"] == expected

    async def test_a_rec_with_no_organization_did_sends_no_authority(
        self, monkeypatch, submission, _enable_vc
    ):
        """Better to claim nothing than to name an authority that does not exist.

        `_enable_vc` binds `organization` without `organization_did`, which is a
        legal manifest. Sending an empty `verified_by` would be a credential
        asserting it was verified by nobody.
        """
        di._token_provider = _mock_token_provider()
        bodies: list[dict] = []

        def handler(req):
            if "users/resolve" in str(req.url):
                return httpx.Response(200, json=DERIVE_RESPONSE)
            bodies.append(json.loads(req.content))
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)

        _patch_httpx(monkeypatch, handler)
        await di.provision_user_identity(submission)

        assert "verified_by" not in bodies[0]
        assert bodies[0]["verification_method"] == "submission-review"


class TestTheFunnelDoesNotGuardIssuance:
    async def test_it_never_asks_whether_a_credential_is_already_held(
        self, monkeypatch, submission, _enable_vc
    ):
        """The guard is the wizard's, not the funnel's.

        A manager has just approved *this* submission and the row needs a
        `dataspace_vc_id` of its own to be revocable. ds returns no credential id
        for one it did not just issue, so a reused credential could not be
        recorded and this submission's enablement could never be undone.
        """
        di._token_provider = _mock_token_provider()
        paths: list[str] = []

        def handler(req):
            paths.append(req.url.path)
            if "users/resolve" in str(req.url):
                return httpx.Response(
                    200, json={"subject_id": "sub-1", "did": "did:web:users.example:sub-1"}
                )
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)

        _patch_httpx(monkeypatch, handler)
        await di.provision_user_identity(submission)

        assert "/credentials/check" not in paths
        assert submission.dataspace_vc_id == CREDENTIAL_RESPONSE["credentialId"]

    async def test_one_registry_for_the_whole_flow(self, monkeypatch, submission, _enable_vc):
        """Resolve, issue and the membership share a `RegistryAccess`'s registry.

        Lifting the function briefly made each call build its own, which was the
        door to two calls in one flow addressing two registries. The *token* is
        no longer shared, by design: the credential and the membership are the
        community's acts, each with its own one-scope collector token
        (`test_collector_client.py`); only the resolve is this service's.
        """
        di._token_provider = _mock_token_provider()
        hosts = []

        def handler(req):
            hosts.append(req.url.host)
            return _default_handler(req)

        _patch_httpx(monkeypatch, handler)

        await di.provision_user_identity(submission)

        assert hosts and set(hosts) == {"ir"}


async def test_a_decisions_list_that_repeats_its_cursor_is_not_read_as_complete(monkeypatch):
    """Only a null cursor ends the list; one handed back twice would loop forever
    or, cut short, read as a shorter list of withdrawals."""

    async def _org(alias):
        return {"Authorization": "Bearer org"}

    monkeypatch.setattr(di, "organisation_auth_headers", _org)
    page = {"offer_id": "o", "datasets": ["d"], "limit": 100, "subjects": [], "next_cursor": "c1"}
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json=page))

    with pytest.raises(RuntimeError, match="already issued"):
        await di.get_offer_decisions("o", [OWN_ROUTE])


async def test_the_preregistration_door_also_gets_a_new_id_for_a_spent_did(monkeypatch):
    """A member imported into the registry has no submission: their sharing page
    provisions them from the id this resolve answers. Released by one REC and
    preregistered by the next, they get a new id, and no credential to present."""

    async def resolve(access, params):
        return RESOLVE_SPENT_RESPONSE

    monkeypatch.setattr(di, "_resolve_raw_with_params", resolve)
    access = di.RegistryAccess(base_url="http://ir", headers={})

    resolved, credential = await di.resolve_subject_and_credential(
        access, email="ex-person@example.org"
    )

    assert credential is None
    assert resolved.subject_id != "old-id"
    assert uuid.UUID(resolved.subject_id).version == 4
