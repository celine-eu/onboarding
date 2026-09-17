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


# ── offers controlled by another organisation: the member's own route ─────────
#
# ds records a service's standing share only for a member of the offer's
# controller. An offer controlled elsewhere — the distributor, the operator's
# research — is accepted in the same form, and recorded right after approval with
# the member's own credential. One acceptance; nothing decided for the member.

OWN = "did:web:rec.example"
DISTRIBUTOR = "did:web:dso.example"


@pytest.fixture()
def two_controllers(monkeypatch, submission, _enable_shares, bind_rec):
    """The member accepted one offer of their community's and one of the distributor's."""
    from celine.onboarding.services import template_service

    bind_rec(
        "default",
        organization="rec-example",
        organization_did=OWN,
        linked_participant_did="did:web:rec.example",
    )
    di._token_provider = _mock_token_provider()
    _consented(submission)
    submission.email = "member@example.org"
    submission.data_sharing_consent_offer_ids = ["community-offer", "release-offer"]

    controllers = {"community-offer": "rec-example", "release-offer": "dso-org"}
    dids = {"rec-example": OWN, "dso-org": DISTRIBUTOR}

    async def _offer(rec_slug, offer_id):
        return {"id": offer_id, "recipients": {"controller": controllers[offer_id]}}

    async def _did(alias):
        return dids[alias]

    async def _access():
        return di.RegistryAccess(base_url="http://ir:30005", headers={})

    state = {
        "credential": di.SubjectCredential(subject_id=submission.dataspace_did, vc_jws="vc.jws")
    }

    async def _credential(access, *, email):
        assert email == "member@example.org"
        return state["credential"]

    monkeypatch.setattr(template_service, "get_sharing_offer", _offer)
    monkeypatch.setattr(di, "resolve_consumer_did", _did)
    monkeypatch.setattr(di, "registry_access", _access)
    monkeypatch.setattr(di, "resolve_subject_credential", _credential)

    requests: list[httpx.Request] = []

    def handler(req):
        requests.append(req)
        return httpx.Response(200, json=[{"id": "row"}])

    _patch_httpx(monkeypatch, handler)
    state["requests"] = requests
    return state


def _posts(requests):
    import json

    return [
        (r.url.path, json.loads(r.read().decode()), r.headers.get("X-User-VC"))
        for r in requests
        if r.method == "POST"
    ]


async def test_each_offer_goes_by_the_route_ds_accepts(submission, two_controllers):
    ok = await di.provision_user_shares(submission)

    assert ok is True
    posts = _posts(two_controllers["requests"])
    by_path = {path: (body, vc) for path, body, vc in posts}
    member_body, member_vc = by_path["/consent/my/shares"]
    assert member_body == {"offer_id": "release-offer", "enabled": True}
    assert member_vc == "vc.jws"
    service_body, service_vc = by_path["/consent/admin/shares"]
    assert service_body["offer_id"] == "community-offer"
    assert service_body["legal_basis"]["submission_ref"] == submission.ref
    assert service_vc is None
    assert len(posts) == 2


async def test_no_credential_means_the_distributor_offer_is_not_recorded(
    submission, two_controllers
):
    two_controllers["credential"] = None

    ok = await di.provision_user_shares(submission)

    assert ok is False
    assert submission.share_provisioned is False
    paths = [path for path, _, _ in _posts(two_controllers["requests"])]
    assert "/consent/my/shares" not in paths


async def test_a_credential_for_somebody_else_is_never_presented(submission, two_controllers):
    two_controllers["credential"] = di.SubjectCredential(
        subject_id="did:web:users.example:someone-else", vc_jws="other.jws"
    )

    with pytest.raises(ValueError, match="someone-else"):
        await di.provision_user_shares(submission, raise_on_error=True)
    assert all(vc != "other.jws" for _, _, vc in _posts(two_controllers["requests"]))


async def test_an_offer_whose_controller_cannot_be_told_stays_a_service_share(
    monkeypatch, submission, two_controllers
):
    """Failing to look it up must never select the member's credential."""
    from celine.onboarding.services import template_service

    async def _broken(rec_slug, offer_id):
        raise template_service.SharingOffersUnavailableError("vocabulary down")

    monkeypatch.setattr(template_service, "get_sharing_offer", _broken)

    await di.provision_user_shares(submission)

    paths = [path for path, _, _ in _posts(two_controllers["requests"])]
    assert paths == ["/consent/admin/shares", "/consent/admin/shares"]


async def test_withdrawal_takes_the_route_the_grant_took(submission, two_controllers):
    submission.share_provisioned = True

    ok = await di.withdraw_user_shares(submission, reason="Membership revoked")

    assert ok is True
    by_path = {path: (body, vc) for path, body, vc in _posts(two_controllers["requests"])}
    assert by_path["/consent/my/shares"] == (
        {"offer_id": "release-offer", "enabled": False},
        "vc.jws",
    )
    assert by_path["/consent/admin/shares"][0]["offer_id"] == "community-offer"
    assert by_path["/consent/admin/shares"][0]["enabled"] is False
