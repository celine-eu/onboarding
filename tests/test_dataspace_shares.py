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


@pytest.fixture(autouse=True)
def _org_client(monkeypatch):
    """The community's own client, without a Keycloak to mint its token.

    Only the network hop is stubbed: `organisation_token_provider` still derives
    the client id from the REC's alias and still refuses a missing secret, which
    is the part worth exercising. `used` records what it was asked for, so a test
    can assert this service authenticated as the **community** rather than as
    itself — the whole point of the change.
    """
    from celine.onboarding.services import service_auth

    used: list[tuple[str, str]] = []

    def _provider(client_id: str, client_secret: str):
        used.append((client_id, client_secret))
        return _mock_token_provider()

    monkeypatch.setattr(service_auth, "_provider_for", _provider)
    return used


@pytest.fixture()
def _enable_shares(monkeypatch, bind_rec):
    bind_rec(
        "default",
        organization="rec-example",
        linked_participant_did="did:web:rec.example",
    )
    monkeypatch.setattr(di.settings, "ds_org_client_id", "")
    monkeypatch.setattr(di.settings, "ds_org_client_secret", "org-secret")
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


async def test_shares_provisioned_when_consented(
    monkeypatch, submission, _enable_shares, _org_client
):
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
    # The member ticked the box on a form; this relays their decision. It is the
    # other half of the pair that matters: a relayed withdrawal is then theirs,
    # and no later provisioning run lifts it.
    assert sent["decided_by"] == "subject"
    # Nothing about the person beyond the reference. The community's own
    # connector resolves its members itself, so their supply points stay here.
    assert "keys" not in sent


async def test_the_registration_is_made_as_the_community_not_as_this_service(
    monkeypatch, submission, _enable_shares, _org_client
):
    """ds refuses a plain service token on this route, and it is right to.

    A shared service client is bound to no participant, so one could write a
    consent at any connector for anybody's members. The client here is the
    community's own, derived from the alias its manifest names.
    """
    di._token_provider = _mock_token_provider()
    _consented(submission)
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json=[{"id": "row-1"}]))

    assert await di.provision_user_shares(submission) is True
    assert _org_client == [("svc-ds-connector-rec-example", "org-secret")]


async def test_without_the_organisation_secret_nothing_is_registered(
    monkeypatch, submission, _enable_shares, caplog
):
    """Nothing is sent, and the log names the credential that is missing.

    Sending it anyway would go out as `svc-ds-onboarding` and come back 403, two
    hops away from anything that names the cause. The operator's answer says the
    client is not configured and points at the log, where the setting is: a REC
    manager retrying a share cannot act on a deployment's own settings
    (`services.errors`).
    """
    monkeypatch.setattr(di.settings, "ds_org_client_secret", "")
    di._token_provider = _mock_token_provider()
    _consented(submission)
    posts = []
    _patch_httpx(monkeypatch, lambda req: posts.append(req) or httpx.Response(200, json=[]))

    with caplog.at_level("ERROR"):
        with pytest.raises(ValueError, match="not configured") as raised:
            await di.provision_user_shares(submission, raise_on_error=True)

    assert posts == []
    assert "DS_ORG_CLIENT_SECRET" not in str(raised.value)
    assert "DS_ORG_CLIENT_SECRET" in caplog.text


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
    # Nobody withdrew: the community revoked a membership and the consent goes
    # with it. Recording it as the member's would attribute an act to somebody
    # who did not take it — and lock it, since only they could lift it again.
    assert sent["decided_by"] == "collector"


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


# ── the decision goes to the connector that holds the data ───────────────────
#
# A consent is enforced where the data is served. A community's own connector
# holds its own datasets; the member's meter readings sit at the grid operator,
# and a release decision recorded anywhere else enforces nothing — the data plane
# answering for those rows never reads it. So the community writes at the
# holder's connector, as the collector it has been accepted as, and sends the
# member's supply points with the decision because that connector has no other
# way to find their rows.

HOLDER_URL = "http://dso-connector:30001"
RELEASE = "meter-data-release"
OWN_OFFER = "household-energy-flexibility"
POD = "EX000E00000001"


@pytest.fixture()
def two_connectors(monkeypatch, submission, _enable_shares, bind_rec):
    """The member accepted one of the community's offers and one release offer."""
    bind_rec(
        "default",
        organization="rec-example",
        organization_did="did:web:rec.example",
        linked_participant_did="did:web:rec.example",
        connectors=[{"holder": "example-dso", "url": HOLDER_URL, "offers": [RELEASE]}],
    )
    di._token_provider = _mock_token_provider()
    _consented(submission)
    submission.email = "member@example.org"
    submission.pod_code = POD
    submission.data_sharing_consent_offer_ids = [OWN_OFFER, RELEASE]

    state: dict = {"pods": {submission.dataspace_did: [POD]}}

    async def _supply_points(dids, *, rec_slug):
        return state["pods"]

    from celine.onboarding.services import rec_registry

    monkeypatch.setattr(rec_registry, "supply_points_by_did", _supply_points)

    requests: list[httpx.Request] = []

    def handler(req):
        requests.append(req)
        return httpx.Response(200, json=[{"id": "row", "missing_prerequisites": []}])

    _patch_httpx(monkeypatch, handler)
    state["requests"] = requests
    return state


def _posts(requests):
    import json

    return {str(r.url): json.loads(r.read().decode()) for r in requests if r.method == "POST"}


async def test_each_decision_goes_to_the_connector_that_holds_the_data(submission, two_connectors):
    ok = await di.provision_user_shares(submission)

    assert ok is True
    posts = _posts(two_connectors["requests"])
    assert set(posts) == {
        "http://connector:30001/consent/admin/shares",
        f"{HOLDER_URL}/consent/admin/shares",
    }
    here = posts["http://connector:30001/consent/admin/shares"]
    there = posts[f"{HOLDER_URL}/consent/admin/shares"]
    assert here["offer_id"] == OWN_OFFER
    assert there["offer_id"] == RELEASE
    # One acceptance, on one form. Both are the member's decision, relayed.
    assert here["decided_by"] == there["decided_by"] == "subject"
    assert here["legal_basis"] == there["legal_basis"]


async def test_the_release_decision_carries_the_members_supply_points(submission, two_connectors):
    """Typed keys, and only where they are needed.

    The holder's data plane keys its rows by supply point and knows nothing about
    this community's members, so these are what turn the consent into rows. The
    community's own connector resolves its members without them, and they are
    personal data, so they do not go there.
    """
    await di.provision_user_shares(submission)

    posts = _posts(two_connectors["requests"])
    assert posts[f"{HOLDER_URL}/consent/admin/shares"]["keys"] == [f"pod:{POD}"]
    assert "keys" not in posts["http://connector:30001/consent/admin/shares"]


async def test_a_member_with_no_supply_point_is_not_registered_at_the_holder(
    submission, two_connectors
):
    """A consent that can never yield a row is refused, not recorded.

    The holder finds this member only by the keys sent with the decision. With
    none, the registration would succeed, the member would see a granted toggle,
    and nothing would ever be released — visible to nobody.
    """
    two_connectors["pods"] = {}

    ok = await di.provision_user_shares(submission)

    assert ok is False
    assert submission.share_provisioned is False
    posts = _posts(two_connectors["requests"])
    assert f"{HOLDER_URL}/consent/admin/shares" not in posts
    # The community's own offer is unaffected: one member, two decisions.
    assert "http://connector:30001/consent/admin/shares" in posts


async def test_the_registry_is_asked_before_the_intake_form(submission, two_connectors):
    """Two records of one fact, and the running system is the one that is right.

    A POD an operator corrected or retired in the registry never reaches
    `submissions.pod_code`, so the declared value is a fallback for a deployment
    with no registry and not a second opinion.
    """
    two_connectors["pods"] = {submission.dataspace_did: ["EX000E00000999"]}

    await di.provision_user_shares(submission)

    posts = _posts(two_connectors["requests"])
    assert posts[f"{HOLDER_URL}/consent/admin/shares"]["keys"] == ["pod:EX000E00000999"]


async def test_with_no_registry_the_declared_supply_point_is_used(
    monkeypatch, submission, two_connectors
):
    from celine.onboarding.services import rec_registry

    async def _no_registry(dids, *, rec_slug):
        return None

    monkeypatch.setattr(rec_registry, "supply_points_by_did", _no_registry)

    assert await di.provision_user_shares(submission) is True
    posts = _posts(two_connectors["requests"])
    assert posts[f"{HOLDER_URL}/consent/admin/shares"]["keys"] == [f"pod:{POD}"]


async def test_withdrawal_follows_the_route_the_grant_took(submission, two_connectors):
    submission.share_provisioned = True

    ok = await di.withdraw_user_shares(submission, reason="Membership revoked")

    assert ok is True
    posts = _posts(two_connectors["requests"])
    there = posts[f"{HOLDER_URL}/consent/admin/shares"]
    assert there["offer_id"] == RELEASE
    assert there["enabled"] is False
    assert there["decided_by"] == "collector"
    # ds refuses keys on a withdrawal: a withdrawal drops the keys it had.
    assert "keys" not in there


async def test_a_holder_that_refuses_does_not_hide_behind_the_other_connector(
    monkeypatch, submission, two_connectors
):
    def handler(req):
        two_connectors["requests"].append(req)
        if str(req.url).startswith(HOLDER_URL):
            return httpx.Response(403, text="not an accepted collector")
        return httpx.Response(200, json=[{"id": "row"}])

    _patch_httpx(monkeypatch, handler)

    with pytest.raises(ValueError, match="not an accepted collector"):
        await di.provision_user_shares(submission, raise_on_error=True)
    assert submission.share_provisioned is False


async def test_an_unmet_prerequisite_is_recorded_and_reported(
    monkeypatch, submission, two_connectors, caplog
):
    """ds records the decision and says it admits nobody yet.

    An offer that takes effect only together with another is a real state a
    member can be in — research granted, release not — and it is the explanation
    for "I consented and nothing happened".
    """

    def handler(req):
        two_connectors["requests"].append(req)
        return httpx.Response(200, json=[{"id": "row", "missing_prerequisites": [RELEASE]}])

    _patch_httpx(monkeypatch, handler)

    with caplog.at_level("INFO"):
        assert await di.provision_user_shares(submission) is True
    assert RELEASE in caplog.text


# ── which client the consent is written as ───────────────────────────────────


class TestTheOrganisationClient:
    """Naming it is a convention, not an invention.

    ds creates `svc-ds-connector-<alias>` beside every participant, so deriving
    the id from the community's alias means a deployment configures one secret
    rather than a name it could get subtly wrong.
    """

    def test_the_id_is_derived_from_the_communitys_alias(self, monkeypatch):
        from celine.onboarding.services import service_auth

        monkeypatch.setattr(service_auth.settings, "ds_org_client_id", "")
        assert service_auth.organisation_client_id("example-rec") == "svc-ds-connector-example-rec"

    def test_a_deployment_may_name_its_own(self, monkeypatch):
        from celine.onboarding.services import service_auth

        monkeypatch.setattr(service_auth.settings, "ds_org_client_id", "svc-something-else")
        assert service_auth.organisation_client_id("example-rec") == "svc-something-else"

    def test_a_community_out_of_the_dataspace_has_none(self, monkeypatch):
        """And says so, rather than deriving `svc-ds-connector-`."""
        from celine.onboarding.services import service_auth
        from celine.onboarding.services.errors import ConfigurationError

        monkeypatch.setattr(service_auth.settings, "ds_org_client_id", "")
        with pytest.raises(ConfigurationError, match="dataspace.organization"):
            service_auth.organisation_client_id("")
