"""Block B — data-sharing consent provisioned to the connector after approval.

Covers §3.5: the share is pushed when the person consented and skipped when they
did not; a failed push never tears down a valid identity; retry is explicit and
fails loudly on an unknown offer.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from test_dataspace_identity import (  # reuse the established harness
    CREDENTIAL_RESPONSE,
    DERIVE_RESPONSE,
    _mock_token_provider,
    _patch_httpx,
)

import celine.onboarding.services.dataspace_identity as di


@pytest.fixture(autouse=True)
def _reset_token_provider():
    di._token_provider = None
    yield
    di._token_provider = None


@pytest.fixture()
def _enable_shares(monkeypatch, bind_rec):
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
    monkeypatch.setattr(di.settings, "ds_connector_url", "http://connector:30001")
    monkeypatch.setattr(di.settings, "dataspace_subject_source", "email_hash")
    monkeypatch.setattr(di.settings, "dataspace_user_role", "DataSubject")
    monkeypatch.setattr(di.settings, "dataspace_vc_ttl_days", 365)
    monkeypatch.setattr(di.settings, "dataspace_allowed_actions", "consent.manage")


def _consented(submission):
    submission.dataspace_did = "did:web:users.example:email-abc123"
    submission.data_sharing_consent = True
    submission.data_sharing_consent_at = datetime(2026, 7, 13, 10, tzinfo=UTC)
    submission.data_sharing_consent_offer_ids = ["household-energy-flexibility"]
    submission.data_sharing_consent_text_version = "1.0"
    submission.data_sharing_consent_locale = "it"
    submission.data_sharing_consent_text_sha256 = "sha-of-shown-text"
    submission.share_provisioned = False
    return submission


# ── provision_user_shares ─────────────────────────────────────────────────────


async def test_shares_skipped_when_not_consented(monkeypatch, submission, _enable_shares):
    di._token_provider = _mock_token_provider()
    submission.data_sharing_consent = False
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, json={})

    _patch_httpx(monkeypatch, handler)
    assert await di.provision_user_shares(submission) is False
    assert calls == []


async def test_shares_skipped_without_connector_url(monkeypatch, submission, _enable_shares):
    monkeypatch.setattr(di.settings, "ds_connector_url", "")
    di._token_provider = _mock_token_provider()
    _consented(submission)
    assert await di.provision_user_shares(submission) is False


async def test_shares_provisioned_when_consented(monkeypatch, submission, _enable_shares):
    di._token_provider = _mock_token_provider()
    _consented(submission)
    captured = {}

    def handler(req):
        captured["url"] = str(req.url)
        captured["body"] = req.read().decode()
        return httpx.Response(200, json=[{"id": "row-1", "consumer_id": "*"}])

    _patch_httpx(monkeypatch, handler)
    ok = await di.provision_user_shares(submission)
    assert ok is True
    assert submission.share_provisioned is True
    assert "consent/admin/shares" in captured["url"]
    import json

    sent = json.loads(captured["body"])
    assert sent["subject_id"] == submission.dataspace_did
    assert sent["offer_id"] == "household-energy-flexibility"
    assert sent["enabled"] is True
    assert sent["legal_basis"]["submission_ref"] == submission.ref
    assert sent["legal_basis"]["rendered_text_sha256"] == "sha-of-shown-text"
    assert sent["legal_basis"]["source"] == "onboarding"


async def test_retry_unknown_offer_fails_loudly(monkeypatch, submission, _enable_shares):
    di._token_provider = _mock_token_provider()
    _consented(submission)
    submission.data_sharing_consent_offer_ids = ["no-such-offer"]

    def handler(req):
        return httpx.Response(422, json={"detail": "Unknown sharing offer 'no-such-offer'"})

    _patch_httpx(monkeypatch, handler)
    with pytest.raises(ValueError):
        await di.provision_user_shares(submission, raise_on_error=True)
    assert submission.share_provisioned is False


async def test_share_failure_is_silent_on_approval_path(monkeypatch, submission, _enable_shares):
    """raise_on_error=False (the approval default) never raises."""
    di._token_provider = _mock_token_provider()
    _consented(submission)

    def handler(req):
        return httpx.Response(500, text="connector down")

    _patch_httpx(monkeypatch, handler)
    ok = await di.provision_user_shares(submission)  # must not raise
    assert ok is False
    assert submission.share_provisioned is False


# ── full provision_user_identity: identity survives a share failure ───────────


async def test_approval_survives_share_failure(monkeypatch, submission, _enable_shares):
    di._token_provider = _mock_token_provider()
    submission.data_sharing_consent = True
    submission.data_sharing_consent_at = datetime(2026, 7, 13, 10, tzinfo=UTC)
    submission.data_sharing_consent_offer_ids = ["household-energy-flexibility"]
    submission.data_sharing_consent_text_version = "1.0"
    submission.data_sharing_consent_locale = "it"
    submission.data_sharing_consent_text_sha256 = "sha"
    submission.share_provisioned = False

    def handler(req):
        url = str(req.url)
        if "users/resolve" in url:
            return httpx.Response(200, json=DERIVE_RESPONSE)
        if "credentials/data-subject" in url:
            return httpx.Response(201, json=CREDENTIAL_RESPONSE)
        if "consent/admin/shares" in url:
            return httpx.Response(500, text="connector down")
        return httpx.Response(200, json={})

    _patch_httpx(monkeypatch, handler)
    # No keycloak_user_id → KC sync skipped; no org alias → membership skipped.
    await di.provision_user_identity(submission)

    # Identity is intact despite the share failure — the deliberate deviation.
    assert submission.dataspace_did == CREDENTIAL_RESPONSE["subjectDid"]
    assert submission.dataspace_vc_id == CREDENTIAL_RESPONSE["credentialId"]
    assert submission.share_provisioned is False


# ── withdrawal ────────────────────────────────────────────────────────────────
#
# The mirror of provisioning, and the pair is the point: this service grants on
# the person's behalf, so it withdraws on their behalf. Leaving that undone left
# a consent standing for somebody who is no longer a member — and, because the
# same revocation deletes their credential, no way for them to withdraw it in the
# participant webapp either.


async def test_withdrawal_flips_the_same_call_it_granted_with(
    monkeypatch, submission, _enable_shares
):
    di._token_provider = _mock_token_provider()
    _consented(submission)
    submission.share_provisioned = True
    captured = {}

    def handler(req):
        captured["url"] = str(req.url)
        captured["body"] = req.read().decode()
        return httpx.Response(200, json=[{"id": "row-1", "status": "revoked"}])

    _patch_httpx(monkeypatch, handler)
    ok = await di.withdraw_user_shares(submission, reason="Membership revoked in example")

    assert ok is True
    assert submission.share_provisioned is False
    assert "consent/admin/shares" in captured["url"]

    import json

    sent = json.loads(captured["body"])
    assert sent["subject_id"] == submission.dataspace_did
    assert sent["offer_id"] == "household-energy-flexibility"
    # One boolean apart from the grant. The connector moves the same row to
    # `revoked`; `message` becomes its revocation_reason, which is where *why*
    # is recorded.
    assert sent["enabled"] is False
    assert sent["message"] == "Membership revoked in example"


async def test_withdrawal_is_skipped_when_there_is_nothing_to_withdraw(
    monkeypatch, submission, _enable_shares
):
    di._token_provider = _mock_token_provider()
    _consented(submission)
    submission.data_sharing_consent_offer_ids = []

    def handler(req):  # pragma: no cover — reaching it is the failure
        raise AssertionError("no call should be made when no offers were recorded")

    _patch_httpx(monkeypatch, handler)
    assert await di.withdraw_user_shares(submission) is False


async def test_withdrawal_reports_failure_rather_than_claiming_success(
    monkeypatch, submission, _enable_shares
):
    """`share_provisioned` must not be cleared on a refusal.

    It is the flag the POD export filters on, so clearing it after a failed
    withdrawal would drop the member from exports while their consent is still
    granted in the connector — the two records disagreeing, silently.
    """
    di._token_provider = _mock_token_provider()
    _consented(submission)
    submission.share_provisioned = True

    _patch_httpx(monkeypatch, lambda req: httpx.Response(500, text="boom"))
    assert await di.withdraw_user_shares(submission) is False
    assert submission.share_provisioned is True
