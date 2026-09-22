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
    monkeypatch.setattr(di.settings, "dataspace_user_role", "DataSubject")
    monkeypatch.setattr(di.settings, "dataspace_vc_ttl_days", 365)
    monkeypatch.setattr(di.settings, "dataspace_allowed_actions", "consent.manage")


def _patch_connector(monkeypatch, handler):
    """``handler`` answers the writes; the reads answer what it accepted.

    Provisioning reads every connector again after writing, to catch a decision
    that changed meanwhile, so a connector that forgets what it accepted looks
    like one that never records it. This remembers each accepted write, per
    connector and offer, the way ds would list it back.
    """
    import json

    recorded: dict[tuple[str, str], str] = {}

    def wrapped(req):
        url = str(req.url)
        base = url.split("/consent/", 1)[0]
        if req.method == "GET" and "/consent/admin/subject-shares" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "offer_id": offer,
                        "status": status,
                        "decided_by": "subject",
                        "collector": "did:web:rec.example",
                    }
                    for (where, offer), status in recorded.items()
                    if where == base
                ],
            )
        resp = handler(req)
        if req.method == "POST" and resp.status_code < 400:
            body = json.loads(req.read().decode())
            recorded[(base, body["offer_id"])] = "granted" if body["enabled"] else "revoked"
        return resp

    _patch_httpx(monkeypatch, wrapped)


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

    _patch_connector(monkeypatch, handler)
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
    _patch_connector(monkeypatch, lambda req: httpx.Response(200, json=[{"id": "row-1"}]))

    assert await di.provision_user_shares(submission) is True
    # Every call — the read of what is already recorded, and the write — is the
    # community's; nothing is made as this service.
    assert _org_client
    assert set(_org_client) == {("svc-ds-connector-rec-example", "org-secret")}


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
    monkeypatch, submission, _enable_shares, bind_rec
):
    """The body is pinned, key for key: ds's ``AdminShareRequest`` forbids extras.

    It used to carry ``message``, which ds never accepted on this route — every
    revocation's share step was a 422 (found live, 2026-09-19). ds's field is
    ``reason`` (ADR-0019 there), accepted only on the organisation's own
    withdrawal, and it lands on the row's ``revocation_reason``.
    """
    bind_rec("default", organization="rec-example", organization_did="did:web:rec.example")
    di._token_provider = _mock_token_provider()
    _consented(submission)
    submission.share_provisioned = True
    posted: list[dict] = []

    def handler(req):
        import json

        if req.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "offer_id": "household-energy-flexibility",
                        "status": "granted",
                        "decided_by": "subject",
                        "collector": "did:web:rec.example",
                        "consumer_id": "*",
                    }
                ],
            )
        assert "consent/admin/shares" in str(req.url)
        posted.append(json.loads(req.read().decode()))
        return httpx.Response(200, json=[{"id": "row-1", "status": "revoked"}])

    _patch_httpx(monkeypatch, handler)
    ok = await di.withdraw_user_shares(submission, reason="Membership revoked in example")

    assert ok is True
    assert submission.share_provisioned is False
    # One boolean apart from the grant, and the cause in the field ds records it
    # from. Nobody withdrew: the community revoked a membership and the consent
    # goes with it — recorded as the member's, it would be attributed to somebody
    # who did not take it, and locked, since only they could lift it again.
    assert posted == [
        {
            "subject_id": submission.dataspace_did,
            "offer_id": "household-energy-flexibility",
            "enabled": False,
            "decided_by": "collector",
            "reason": "Membership revoked in example",
        }
    ]
    assert "message" not in posted[0]


async def test_nothing_standing_is_nothing_to_withdraw(
    monkeypatch, submission, _enable_shares, bind_rec
):
    """Reversed on 2026-09-19 (the maintainer): an empty form no longer means no call.

    A member who accepted nothing on the form may have granted on their sharing
    page since, so the connectors are read whatever the form said. Nothing
    standing there is nothing to write, and that is a complete revocation.
    """
    bind_rec("default", organization="rec-example", organization_did="did:web:rec.example")
    di._token_provider = _mock_token_provider()
    _consented(submission)
    submission.data_sharing_consent_offer_ids = []
    reads: list[str] = []

    def handler(req):
        assert req.method == "GET", "nothing stands, so nothing is written"
        reads.append(str(req.url))
        return httpx.Response(200, json=[])

    _patch_httpx(monkeypatch, handler)
    assert await di.withdraw_user_shares(submission) is True
    assert len(reads) == 1 and "subject-shares" in reads[0]


async def test_without_a_dataspace_identity_nothing_is_called(
    monkeypatch, submission, _enable_shares
):
    di._token_provider = _mock_token_provider()
    _consented(submission)
    submission.dataspace_did = None

    def handler(req):  # pragma: no cover — reaching it is the failure
        raise AssertionError("no subject, nothing to read or withdraw")

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
#: The community's organisation DID: what ds stamps as ``collector`` on every row
#: its organisation client writes, here or at a holder.
COMMUNITY_DID = "did:web:rec.example"
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

    _patch_connector(monkeypatch, handler)
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
    """It follows what stands, read from each connector — not the form's list.

    Changed on 2026-09-19 (the maintainer): it used to post a withdrawal for
    every offer on the form, whether or not anything stood.
    """
    assert await di.provision_user_shares(submission) is True
    two_connectors["requests"].clear()

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

    _patch_connector(monkeypatch, handler)

    with caplog.at_level("INFO"):
        assert await di.provision_user_shares(submission) is True
    assert RELEASE in caplog.text


# ── one offer, several connectors (ADR-0007) ─────────────────────────────────
#
# An offer whose data sits in two places — the grid operator's readings and the
# community's own meter datasets — is recorded at both. The fake connectors below
# keep state, because the properties worth proving are about a *second* run: a
# retry writes only what the first run missed, and never lifts a withdrawal the
# member made in between.

OWN_URL = "http://connector:30001"
RESEARCH = "forecasting-and-research"


class _Connectors:
    """Two connectors that remember what they recorded, per (connector, offer).

    They keep time the way ds does, because the retry ranks decisions by it: a
    grant stamps ``decided_at``; withdrawing a standing grant **mutates** that row
    — ``revoked_at`` is stamped and ``decided_at`` keeps the grant's time — and a
    withdrawal over a withdrawal changes nothing but whose it is (a subject
    repeating "stop" makes it theirs). Evidence sent with a grant is stored on
    the row and read back, as ds's ``legal_basis`` is.
    """

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.requests: list[httpx.Request] = []
        #: base URL → status the POST answers instead of recording (e.g. 502)
        self.refuse: dict[str, int] = {}
        #: base URL → status the subject-shares read answers instead
        self.unreadable: dict[str, int] = {}
        #: Called once, just before the first POST is recorded — the moment a
        #: member acting between the retry's read and its write would land.
        self.before_first_post = None
        self.clock = datetime(2026, 9, 19, 10, tzinfo=UTC)

    def tick(self) -> str:
        from datetime import timedelta

        self.clock += timedelta(minutes=1)
        return self.clock.isoformat()

    def decide(
        self,
        base: str,
        offer: str,
        *,
        granted: bool,
        decided_by: str = "subject",
        collector: str | None = None,
        **extra,
    ) -> None:
        """Record a decision taken *outside* the code under test, as ds would.

        ``collector`` is the organisation whose token wrote it, stamped on the
        row the decision lands on, as ds does (``None``: the member at their own
        connector with their own credential).
        """
        now = self.tick()
        row = self.rows.get((base, offer))
        reason = extra.pop("reason", None)
        if granted:
            self.rows[(base, offer)] = {
                "status": "granted",
                "decided_by": decided_by,
                "collector": collector,
                "consumer_id": "*",
                "keys": extra.pop("keys", None),
                "requested_at": now,
                "decided_at": now,
                "revoked_at": None,
                "legal_basis": extra.pop("legal_basis", None),
                **extra,
            }
        elif row is not None and row["status"] == "granted":
            row.update(
                status="revoked",
                revoked_at=now,
                decided_by=decided_by,
                collector=collector,
                keys=None,
                revocation_reason=reason,
            )
        elif row is not None:
            if decided_by == "subject":
                row["decided_by"] = "subject"
                row["collector"] = collector
        else:
            self.rows[(base, offer)] = {
                "status": "revoked",
                "decided_by": decided_by,
                "collector": collector,
                "consumer_id": "*",
                "keys": None,
                "requested_at": now,
                "decided_at": None,
                "revoked_at": now,
                "revocation_reason": reason,
                "legal_basis": None,
            }

    def handler(self, req: httpx.Request) -> httpx.Response:
        import json

        self.requests.append(req)
        url = str(req.url)
        base = url.split("/consent/", 1)[0]
        if req.method == "GET" and "/consent/admin/subject-shares" in url:
            if base in self.unreadable:
                return httpx.Response(self.unreadable[base], text="unavailable")
            return httpx.Response(
                200,
                json=[
                    {"offer_id": offer, **row}
                    for (where, offer), row in self.rows.items()
                    if where == base
                ],
            )
        if req.method == "POST" and url.endswith("/consent/admin/shares"):
            if self.before_first_post is not None:
                hook, self.before_first_post = self.before_first_post, None
                hook()
            if base in self.refuse:
                return httpx.Response(self.refuse[base], text="refused")
            body = json.loads(req.read().decode())
            row = self.rows.get((base, body["offer_id"]))
            if body["enabled"] and row is not None and row["status"] == "granted":
                if body.get("keys") is not None:
                    row["keys"] = body["keys"]
            else:
                self.decide(
                    base,
                    body["offer_id"],
                    granted=body["enabled"],
                    decided_by=body["decided_by"],
                    # Every write here is the community's organisation client.
                    collector=COMMUNITY_DID,
                    keys=body.get("keys") if body["enabled"] else None,
                    legal_basis=body.get("legal_basis"),
                    reason=body.get("reason"),
                )
            return httpx.Response(200, json=[{"id": "row", "missing_prerequisites": []}])
        raise AssertionError(f"unexpected call {req.method} {url}")

    def posts(self) -> list[tuple[str, dict]]:
        import json

        return [
            (str(r.url).rsplit("/consent/", 1)[0], json.loads(r.read().decode()))
            for r in self.requests
            if r.method == "POST"
        ]


@pytest.fixture()
def held_twice(monkeypatch, submission, _enable_shares, bind_rec):
    """Research is held by the grid operator and by the community itself."""
    bind_rec(
        "default",
        organization="rec-example",
        organization_did="did:web:rec.example",
        linked_participant_did="did:web:rec.example",
        connectors=[
            {"holder": "example-dso", "url": HOLDER_URL, "offers": [RELEASE, RESEARCH]},
            {"holder": "rec-example", "offers": [RESEARCH]},
        ],
    )
    di._token_provider = _mock_token_provider()
    _consented(submission)
    submission.pod_code = POD
    submission.data_sharing_consent_offer_ids = [OWN_OFFER, RELEASE, RESEARCH]

    async def _supply_points(dids, *, rec_slug):
        return {submission.dataspace_did: [POD]}

    from celine.onboarding.services import rec_registry

    monkeypatch.setattr(rec_registry, "supply_points_by_did", _supply_points)
    fake = _Connectors()
    _patch_httpx(monkeypatch, fake.handler)
    return fake


async def test_an_offer_held_in_two_places_is_recorded_at_both(submission, held_twice):
    assert await di.provision_user_shares(submission) is True
    assert submission.share_provisioned is True

    rows = held_twice.rows
    assert rows[(OWN_URL, RESEARCH)]["status"] == "granted"
    assert rows[(HOLDER_URL, RESEARCH)]["status"] == "granted"
    # The release is the grid operator's alone, the flexibility offer the community's.
    assert (OWN_URL, RELEASE) not in rows
    assert (HOLDER_URL, OWN_OFFER) not in rows
    # Keys go only to the other participant, for every offer it holds.
    assert rows[(HOLDER_URL, RESEARCH)]["keys"] == [f"pod:{POD}"]
    assert rows[(HOLDER_URL, RELEASE)]["keys"] == [f"pod:{POD}"]
    assert rows[(OWN_URL, RESEARCH)]["keys"] is None
    assert rows[(OWN_URL, OWN_OFFER)]["keys"] is None
    # All of it the member's decision, relayed.
    assert {row["decided_by"] for row in rows.values()} == {"subject"}


async def test_a_half_that_is_refused_fails_the_step_and_the_other_half_lands(
    submission, held_twice
):
    held_twice.refuse[HOLDER_URL] = 502

    with pytest.raises(ValueError, match="example-dso's connector: 502"):
        await di.provision_user_shares(submission, raise_on_error=True)

    assert submission.share_provisioned is False
    assert held_twice.rows[(OWN_URL, RESEARCH)]["status"] == "granted"
    assert (HOLDER_URL, RESEARCH) not in held_twice.rows


async def test_a_retry_writes_only_what_is_missing(submission, held_twice):
    held_twice.refuse[HOLDER_URL] = 502
    assert await di.provision_user_shares(submission) is False
    held_twice.refuse.clear()
    held_twice.requests.clear()

    assert await di.provision_user_shares(submission, raise_on_error=True) is True

    # Only the grid operator's half is written again; the community's connector,
    # which already records both of its offers, is read and left alone.
    assert sorted((base, body["offer_id"]) for base, body in held_twice.posts()) == sorted(
        [(HOLDER_URL, RELEASE), (HOLDER_URL, RESEARCH)]
    )
    assert held_twice.rows[(HOLDER_URL, RESEARCH)]["keys"] == [f"pod:{POD}"]
    assert submission.share_provisioned is True


async def test_a_retry_is_idempotent_where_everything_is_recorded(submission, held_twice):
    assert await di.provision_user_shares(submission) is True
    held_twice.requests.clear()

    assert await di.provision_user_shares(submission, raise_on_error=True) is True
    assert held_twice.posts() == []


async def test_a_retry_never_lifts_a_withdrawal_the_member_made(submission, held_twice):
    """ds lets the subject re-open their own refusal, and this relays the subject.

    So a retry that re-posted the form's grant would silently undo a member who
    withdrew research at their community after the grid operator's half failed.
    """
    held_twice.refuse[HOLDER_URL] = 502
    assert await di.provision_user_shares(submission) is False
    # Meanwhile the member withdrew research at their own community.
    held_twice.decide(OWN_URL, RESEARCH, granted=False)
    held_twice.refuse.clear()
    held_twice.requests.clear()

    await di.provision_user_shares(submission, raise_on_error=True)

    assert (OWN_URL, RESEARCH) not in [(b, body["offer_id"]) for b, body in held_twice.posts()]
    assert held_twice.rows[(OWN_URL, RESEARCH)]["status"] == "revoked"


async def test_the_communitys_own_withdrawal_is_lifted_on_re_approval(submission, held_twice):
    """A collector's withdrawal (revoked membership) is the community's to re-open.

    It is newer than the form, and still does not outrank it: it is not the
    member's decision, and re-approving is the community deciding again.
    """
    held_twice.decide(OWN_URL, RESEARCH, granted=False, decided_by="collector")

    assert await di.provision_user_shares(submission) is True
    assert held_twice.rows[(OWN_URL, RESEARCH)]["status"] == "granted"


async def test_a_connector_that_cannot_be_read_fails_and_nothing_is_written(submission, held_twice):
    """Reversed on 2026-09-19 (the maintainer): the community's half used to land.

    The newest decision is read across every connector holding an offer, so one
    that cannot be read leaves it unknown — and the half written anyway could be
    a grant over a withdrawal made at the connector nobody could read.
    """
    held_twice.unreadable[HOLDER_URL] = 503

    with pytest.raises(ValueError, match="could not read what example-dso's connector"):
        await di.provision_user_shares(submission, raise_on_error=True)

    assert held_twice.posts() == []
    assert held_twice.rows == {}
    assert submission.share_provisioned is False


async def test_revocation_withdraws_at_every_connector_holding_the_offer(submission, held_twice):
    assert await di.provision_user_shares(submission) is True

    assert await di.withdraw_user_shares(submission, reason="Membership revoked") is True

    for where in (OWN_URL, HOLDER_URL):
        row = held_twice.rows[(where, RESEARCH)]
        assert row["status"] == "revoked", where
        assert row["decided_by"] == "collector", where
        assert row["keys"] is None, where
    assert held_twice.rows[(HOLDER_URL, RELEASE)]["status"] == "revoked"
    assert submission.share_provisioned is False


async def test_a_withdrawal_one_connector_refuses_still_reaches_the_other(submission, held_twice):
    """A withdrawal that reaches one holder of two is a leak, not a lag."""
    assert await di.provision_user_shares(submission) is True
    held_twice.refuse[HOLDER_URL] = 502

    with pytest.raises(ValueError, match="example-dso's connector: 502"):
        await di.withdraw_user_shares(submission, raise_on_error=True)

    assert held_twice.rows[(OWN_URL, RESEARCH)]["status"] == "revoked"
    # Still standing where it was refused — and reported, not cleared.
    assert held_twice.rows[(HOLDER_URL, RESEARCH)]["status"] == "granted"
    assert submission.share_provisioned is True


# ── revocation withdraws every grant the community collected ─────────────────
#
# The maintainer, 2026-09-19: when a membership is revoked, every grant the
# community collected for that member is withdrawn, whether it came from the form
# or from the member's sharing page. What stands is read from every connector the
# manifest names — the form's offer list is only what the member accepted once —
# and a grant somebody else collected is not the community's to withdraw.

OTHER_COLLECTOR = "did:web:other-rec.example"


class TestRevocationWithdrawsEveryGrant:
    async def test_a_grant_made_only_on_the_page_is_withdrawn(self, submission, held_twice):
        """Research was never on the form; the member granted it on their page."""
        submission.data_sharing_consent_offer_ids = [OWN_OFFER]
        assert await di.provision_user_shares(submission) is True
        # The page: the community's half as the member themselves, the holder's
        # relayed by the community.
        held_twice.decide(OWN_URL, RESEARCH, granted=True, collector=None)
        held_twice.decide(HOLDER_URL, RESEARCH, granted=True, collector=COMMUNITY_DID)

        assert await di.withdraw_user_shares(submission, raise_on_error=True) is True

        for where in (OWN_URL, HOLDER_URL):
            row = held_twice.rows[(where, RESEARCH)]
            assert row["status"] == "revoked", where
            assert row["decided_by"] == "collector", where
        assert held_twice.rows[(OWN_URL, OWN_OFFER)]["status"] == "revoked"

    async def test_a_member_who_declined_the_form_but_granted_on_the_page_is_withdrawn(
        self, submission, held_twice
    ):
        submission.data_sharing_consent = False
        submission.data_sharing_consent_offer_ids = []
        held_twice.decide(OWN_URL, RESEARCH, granted=True, collector=None)
        held_twice.decide(HOLDER_URL, RESEARCH, granted=True, collector=COMMUNITY_DID)
        held_twice.decide(HOLDER_URL, RELEASE, granted=True, collector=COMMUNITY_DID)

        assert await di.withdraw_user_shares(submission, raise_on_error=True) is True

        assert {row["status"] for row in held_twice.rows.values()} == {"revoked"}
        assert sorted((b, body["offer_id"]) for b, body in held_twice.posts()) == sorted(
            [(OWN_URL, RESEARCH), (HOLDER_URL, RESEARCH), (HOLDER_URL, RELEASE)]
        )

    async def test_a_grant_another_party_collected_is_left_alone(self, submission, held_twice):
        """ds would let the write through; the community still may not make it.

        ``POST /consent/admin/shares`` withdraws whatever grant stands in the
        cell, whoever collected it, once the subject is also the caller's member.
        So the rule is onboarding's: a membership of *this* community ending says
        nothing about the member's decision collected by another organisation —
        and the withdrawal would be stamped as ours, one only we or the member
        could lift (``_may_lift``).
        """
        held_twice.decide(HOLDER_URL, RELEASE, granted=True, collector=OTHER_COLLECTOR)
        held_twice.decide(OWN_URL, RESEARCH, granted=True, collector=OTHER_COLLECTOR)
        held_twice.decide(HOLDER_URL, RESEARCH, granted=True, collector=COMMUNITY_DID)

        assert await di.withdraw_user_shares(submission, raise_on_error=True) is True

        assert held_twice.posts() == [
            (
                HOLDER_URL,
                {
                    "subject_id": submission.dataspace_did,
                    "offer_id": RESEARCH,
                    "enabled": False,
                    "decided_by": "collector",
                    "reason": "Membership revoked in default",
                },
            )
        ]
        assert held_twice.rows[(HOLDER_URL, RELEASE)]["status"] == "granted"
        assert held_twice.rows[(OWN_URL, RESEARCH)]["status"] == "granted"

    async def test_a_members_own_withdrawal_is_not_written_over(self, submission, held_twice):
        """Only a standing grant is withdrawn: a refusal the member made stays theirs."""
        assert await di.provision_user_shares(submission) is True
        held_twice.decide(OWN_URL, RESEARCH, granted=False, collector=None)
        held_twice.requests.clear()

        assert await di.withdraw_user_shares(submission, raise_on_error=True) is True

        assert (OWN_URL, RESEARCH) not in [(b, body["offer_id"]) for b, body in held_twice.posts()]
        assert held_twice.rows[(OWN_URL, RESEARCH)]["decided_by"] == "subject"

    async def test_every_connector_the_manifest_names_is_read_and_withdrawn_at(
        self, monkeypatch, submission, held_twice, bind_rec
    ):
        """Fan-out: the community's own connector and two other holders."""
        bind_rec(
            "default",
            organization="rec-example",
            organization_did=COMMUNITY_DID,
            linked_participant_did=COMMUNITY_DID,
            connectors=[
                {"holder": "example-dso", "url": HOLDER_URL, "offers": [RELEASE, RESEARCH]},
                {"holder": "example-dso-2", "url": SECOND_HOLDER_URL, "offers": [RESEARCH]},
                {"holder": "rec-example", "offers": [RESEARCH]},
            ],
        )
        assert await di.provision_user_shares(submission) is True
        assert (SECOND_HOLDER_URL, RESEARCH) in held_twice.rows
        held_twice.requests.clear()

        assert await di.withdraw_user_shares(submission, raise_on_error=True) is True

        reads = {
            str(r.url).split("/consent/", 1)[0] for r in held_twice.requests if r.method == "GET"
        }
        assert reads == {OWN_URL, HOLDER_URL, SECOND_HOLDER_URL}
        assert sorted((b, body["offer_id"]) for b, body in held_twice.posts()) == sorted(
            [
                (OWN_URL, OWN_OFFER),
                (OWN_URL, RESEARCH),
                (HOLDER_URL, RELEASE),
                (HOLDER_URL, RESEARCH),
                (SECOND_HOLDER_URL, RESEARCH),
            ]
        )
        assert {row["status"] for row in held_twice.rows.values()} == {"revoked"}

    async def test_an_unreadable_connector_fails_and_a_retry_completes(
        self, submission, held_twice
    ):
        """What stands there is unknown, so the revocation cannot be complete.

        The readable connectors are withdrawn at anyway: a withdrawal never lifts
        anything, so unlike a grant it is safe to write without the whole
        picture — and every connector withdrawn at is one fewer still serving.
        """
        assert await di.provision_user_shares(submission) is True
        held_twice.unreadable[HOLDER_URL] = 503

        with pytest.raises(ValueError, match="could not read what example-dso's connector"):
            await di.withdraw_user_shares(submission, raise_on_error=True)

        assert held_twice.rows[(OWN_URL, RESEARCH)]["status"] == "revoked"
        assert held_twice.rows[(HOLDER_URL, RESEARCH)]["status"] == "granted"
        assert submission.share_provisioned is True

        held_twice.unreadable.clear()
        held_twice.requests.clear()
        assert await di.withdraw_user_shares(submission, raise_on_error=True) is True

        # The retry writes only what still stands.
        assert sorted((b, body["offer_id"]) for b, body in held_twice.posts()) == sorted(
            [(HOLDER_URL, RELEASE), (HOLDER_URL, RESEARCH)]
        )
        assert {row["status"] for row in held_twice.rows.values()} == {"revoked"}
        assert submission.share_provisioned is False

    async def test_the_members_recorded_intent_is_left_as_it_is(
        self, submission, held_twice, intents
    ):
        """A collector's withdrawal is not the member's decision, so it is not recorded as one.

        Their intent is theirs; a re-approval ranks it as it ranks the form, and a
        collector's row never outranks either.
        """
        from celine.onboarding.services import sharing_intent

        submission.data_sharing_consent_offer_ids = []
        submission.data_sharing_consent = False
        await sharing_intent.record(
            subject_id=submission.dataspace_did,
            offer_id=RESEARCH,
            rec_slug=submission.rec_slug,
            granted=True,
            evidence=None,
        )
        before = dict(await sharing_intent.for_subject(submission.dataspace_did))
        held_twice.decide(OWN_URL, RESEARCH, granted=True, collector=None)

        assert await di.withdraw_user_shares(submission, raise_on_error=True) is True

        assert held_twice.rows[(OWN_URL, RESEARCH)]["status"] == "revoked"
        assert dict(await sharing_intent.for_subject(submission.dataspace_did)) == before

    async def test_the_reason_is_recorded_on_the_row(self, submission, held_twice):
        assert await di.provision_user_shares(submission) is True

        assert await di.withdraw_user_shares(submission, raise_on_error=True) is True

        assert {row["revocation_reason"] for row in held_twice.rows.values()} == {
            "Membership revoked in default"
        }

    async def test_without_a_manifest_did_the_registry_says_which_grants_are_ours(
        self, monkeypatch, submission, held_twice, bind_rec
    ):
        """The manifest's ``organization_did`` is optional; the collector is not."""
        bind_rec(
            "default",
            organization="rec-example",
            connectors=[
                {"holder": "example-dso", "url": HOLDER_URL, "offers": [RELEASE, RESEARCH]},
                {"holder": "rec-example", "offers": [RESEARCH]},
            ],
        )
        answer = {"did": COMMUNITY_DID}

        async def _check(alias):
            assert alias == "rec-example"
            return di.OwnerCheck(found=answer["did"] is not None, did=answer["did"])

        monkeypatch.setattr(di, "check_organization", _check)
        held_twice.decide(HOLDER_URL, RESEARCH, granted=True, collector=COMMUNITY_DID)
        held_twice.decide(HOLDER_URL, RELEASE, granted=True, collector=OTHER_COLLECTOR)

        answer["did"] = None
        with pytest.raises(ValueError, match="cannot be told apart"):
            await di.withdraw_user_shares(submission, raise_on_error=True)
        assert held_twice.posts() == []

        answer["did"] = COMMUNITY_DID
        assert await di.withdraw_user_shares(submission, raise_on_error=True) is True
        assert [(b, body["offer_id"]) for b, body in held_twice.posts()] == [(HOLDER_URL, RESEARCH)]


SECOND_HOLDER_URL = "http://other-dso-connector:30001"


class TestTheReason:
    """ds's limits (``AdminShareRequest.reason``): 1–200 characters after
    trimming, one line, no control character, no ``@``."""

    def test_the_default_is_sent_as_is(self):
        assert di.withdrawal_reason("Membership revoked in example") == (
            "Membership revoked in example"
        )

    def test_it_is_one_line(self):
        assert di.withdrawal_reason("  Membership\nrevoked\tin\x00example \x7f") == (
            "Membership revoked in example"
        )

    def test_it_is_at_most_200_characters(self):
        cut = di.withdrawal_reason("x" * 500)
        assert cut == "x" * 200

    def test_an_address_is_never_sent(self):
        assert di.withdrawal_reason("revoked for member@example.org") == "Membership revoked"

    def test_nothing_left_is_no_reason(self):
        assert di.withdrawal_reason(" \n\t ") is None


class TestOnlyTheCommunitysOwnWithdrawalCarriesAReason:
    """ds refuses a reason anywhere else (422); onboarding never builds one."""

    @pytest.mark.parametrize(
        ("enabled", "decided_by"), [(True, "subject"), (True, "collector"), (False, "subject")]
    )
    async def test_refused_before_sending(self, monkeypatch, _enable_shares, enabled, decided_by):
        def handler(req):  # pragma: no cover — reaching it is the failure
            raise AssertionError("nothing may be sent")

        _patch_httpx(monkeypatch, handler)
        route = di.ConsentRoute(offer_id=RESEARCH, connector_url=OWN_URL, collector="rec-example")
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="reason"):
                await di.register_share(
                    client,
                    route,
                    subject_id="did:web:users.example:x",
                    enabled=enabled,
                    decided_by=decided_by,
                    reason="Membership revoked",
                )

    async def test_no_other_write_sends_one(self, submission, held_twice):
        """Grants, relayed withdrawals and re-drives: no ``reason``, and never ``message``."""
        assert await di.provision_user_shares(submission) is True
        held_twice.decide(HOLDER_URL, RESEARCH, granted=False)  # a split to re-drive
        await di.provision_user_shares(submission, raise_on_error=True)

        bodies = [body for _, body in held_twice.posts()]
        assert any(body["enabled"] is False for body in bodies)
        assert all("reason" not in body and "message" not in body for body in bodies)


@pytest.fixture()
def held_apart(monkeypatch, submission, _enable_shares, bind_rec):
    """No offer in two places: the release at the grid operator, one offer here.

    The retry properties do not depend on an offer being held twice — a member
    who accepted one offer of each kind has two connectors too.
    """
    bind_rec(
        "default",
        organization="rec-example",
        organization_did="did:web:rec.example",
        linked_participant_did="did:web:rec.example",
        connectors=[{"holder": "example-dso", "url": HOLDER_URL, "offers": [RELEASE]}],
    )
    di._token_provider = _mock_token_provider()
    _consented(submission)
    submission.pod_code = POD
    submission.data_sharing_consent_offer_ids = [OWN_OFFER, RELEASE]

    async def _supply_points(dids, *, rec_slug):
        return {submission.dataspace_did: [POD]}

    from celine.onboarding.services import rec_registry

    monkeypatch.setattr(rec_registry, "supply_points_by_did", _supply_points)
    fake = _Connectors()
    _patch_httpx(monkeypatch, fake.handler)
    return fake


async def test_a_retry_after_the_holder_refused_writes_only_the_holders_half(
    submission, held_apart
):
    held_apart.refuse[HOLDER_URL] = 502
    assert await di.provision_user_shares(submission) is False
    held_apart.refuse.clear()
    held_apart.requests.clear()

    assert await di.provision_user_shares(submission, raise_on_error=True) is True
    assert [(b, body["offer_id"]) for b, body in held_apart.posts()] == [(HOLDER_URL, RELEASE)]


async def test_a_retry_never_lifts_a_withdrawal_the_member_made_at_their_community(
    submission, held_apart
):
    held_apart.refuse[HOLDER_URL] = 502
    assert await di.provision_user_shares(submission) is False
    held_apart.decide(OWN_URL, OWN_OFFER, granted=False)
    held_apart.refuse.clear()

    assert await di.provision_user_shares(submission, raise_on_error=True) is True
    assert held_apart.rows[(OWN_URL, OWN_OFFER)]["status"] == "revoked"
    assert held_apart.rows[(HOLDER_URL, RELEASE)]["status"] == "granted"


# ── the retry re-drives a split (the maintainer, 2026-09-19) ─────────────────
#
# The retry reads the member's newest decision across every connector holding an
# offer and applies it at each, withdrawals included. The newest decision wins —
# a collector's withdrawal is not the member's and never outranks one — and a
# grant the retry writes carries the time of the decision it relays, so it can
# never outrank a withdrawal the member made after that decision.

#: What a member's own grant, relayed to a holder from their sharing page, stores.
RELAYED_EVIDENCE = {
    "source": "onboarding-member",
    "rec_slug": "default",
    "consent_text_version": "2.0",
    "locale": "it",
    "rendered_text_sha256": "sha-of-what-the-page-served",
}


def _research(fake: _Connectors) -> dict[str, str]:
    return {where: fake.rows[(where, RESEARCH)]["status"] for where in (OWN_URL, HOLDER_URL)}


class TestTheRetryReDrivesASplit:
    @pytest.mark.parametrize(
        ("withdrawn_at", "other"), [(OWN_URL, HOLDER_URL), (HOLDER_URL, OWN_URL)]
    )
    async def test_a_withdrawal_split_is_re_driven_to_withdrawn_everywhere(
        self, submission, held_twice, withdrawn_at, other
    ):
        assert await di.provision_user_shares(submission) is True
        # The member's withdrawal landed at one connector; the other refused it.
        held_twice.decide(withdrawn_at, RESEARCH, granted=False)
        held_twice.requests.clear()

        assert await di.provision_user_shares(submission, raise_on_error=True) is True

        assert _research(held_twice) == {OWN_URL: "revoked", HOLDER_URL: "revoked"}
        [(where, body)] = held_twice.posts()
        assert where == other
        assert body["offer_id"] == RESEARCH
        assert body["enabled"] is False
        # Still the member's decision, relayed — never the community's.
        assert body["decided_by"] == "subject"
        assert "keys" not in body
        # ds's `AdminShareRequest` is `extra="forbid"` and has no `message`: a
        # relayed withdrawal carrying one is refused 422 (found live).
        assert set(body) <= {"subject_id", "offer_id", "enabled", "decided_by"}
        assert held_twice.rows[(other, RESEARCH)]["decided_by"] == "subject"
        # The offers that were never split are left alone.
        assert held_twice.rows[(HOLDER_URL, RELEASE)]["status"] == "granted"
        assert held_twice.rows[(OWN_URL, OWN_OFFER)]["status"] == "granted"

    async def test_a_grant_split_is_re_driven_to_granted(self, submission, held_twice):
        assert await di.provision_user_shares(submission) is True
        for where in (OWN_URL, HOLDER_URL):
            held_twice.decide(where, RESEARCH, granted=False)
        # The member granted research again; only the holder's half landed.
        held_twice.decide(
            HOLDER_URL,
            RESEARCH,
            granted=True,
            keys=[f"pod:{POD}"],
            legal_basis=dict(RELAYED_EVIDENCE),
        )
        regranted_at = held_twice.rows[(HOLDER_URL, RESEARCH)]["decided_at"]
        held_twice.requests.clear()

        assert await di.provision_user_shares(submission, raise_on_error=True) is True

        assert _research(held_twice) == {OWN_URL: "granted", HOLDER_URL: "granted"}
        [(where, body)] = held_twice.posts()
        assert where == OWN_URL
        assert body["decided_by"] == "subject"
        assert "keys" not in body  # the community's own connector never gets them
        # The evidence of the decision relayed, stamped with *its* time — not the
        # form's, and not the moment the retry wrote it.
        assert body["legal_basis"]["rendered_text_sha256"] == "sha-of-what-the-page-served"
        assert body["legal_basis"]["source"] == "onboarding-member"
        assert body["legal_basis"]["accepted_at"] == regranted_at

    async def test_a_grant_made_at_the_community_reaches_the_holder_with_its_keys(
        self, submission, held_twice, monkeypatch
    ):
        """The member's own grant carries ds's evidence, which has no hash to relay.

        The holder then gets what the member's page relays for the same act:
        this service's rendering of the offer it served.
        """
        from celine.onboarding.services import template_service

        async def _offer(rec_slug, offer_id):
            return {"id": offer_id, "consent_text_version": "2.0", "requires_consent": True}

        monkeypatch.setattr(template_service, "get_sharing_offer", _offer)

        assert await di.provision_user_shares(submission) is True
        for where in (OWN_URL, HOLDER_URL):
            held_twice.decide(where, RESEARCH, granted=False)
        held_twice.decide(
            OWN_URL,
            RESEARCH,
            granted=True,
            legal_basis={"source": None, "consent_text_version": "2.0", "user_visible_hash": "h"},
        )
        regranted_at = held_twice.rows[(OWN_URL, RESEARCH)]["decided_at"]
        held_twice.requests.clear()

        assert await di.provision_user_shares(submission, raise_on_error=True) is True

        assert _research(held_twice) == {OWN_URL: "granted", HOLDER_URL: "granted"}
        [(where, body)] = held_twice.posts()
        assert where == HOLDER_URL
        assert body["keys"] == [f"pod:{POD}"]
        assert body["legal_basis"]["source"] == "onboarding-member"
        assert body["legal_basis"]["rendered_text_sha256"]
        assert body["legal_basis"]["accepted_at"] == regranted_at

    async def test_a_stale_grant_read_does_not_undo_a_newer_withdrawal(
        self, submission, held_twice
    ):
        """The read-then-write race: the member withdraws after the retry has read.

        The retry read a grant at the community and nothing at the holder, so it
        writes the holder's half — after the member withdrew everywhere. Its
        grant lifts the holder's withdrawal (a relayed `subject` grant may), and
        is then outranked: it relays a decision older than that withdrawal.
        """
        held_twice.refuse[HOLDER_URL] = 502
        assert await di.provision_user_shares(submission) is False
        held_twice.refuse.clear()

        def _member_withdraws():
            for where in (OWN_URL, HOLDER_URL):
                held_twice.decide(where, RESEARCH, granted=False)

        held_twice.before_first_post = _member_withdraws

        assert await di.provision_user_shares(submission, raise_on_error=True) is True

        assert _research(held_twice) == {OWN_URL: "revoked", HOLDER_URL: "revoked"}
        # The release was never withdrawn, and is granted as the form said.
        assert held_twice.rows[(HOLDER_URL, RELEASE)]["status"] == "granted"

    async def test_a_tie_is_a_withdrawal(self, submission, held_twice):
        assert await di.provision_user_shares(submission) is True
        held_twice.decide(OWN_URL, RESEARCH, granted=False)
        withdrawn_at = held_twice.rows[(OWN_URL, RESEARCH)]["revoked_at"]
        held_twice.rows[(HOLDER_URL, RESEARCH)].update(
            decided_at=withdrawn_at, legal_basis=dict(RELAYED_EVIDENCE)
        )

        assert await di.provision_user_shares(submission, raise_on_error=True) is True
        assert _research(held_twice) == {OWN_URL: "revoked", HOLDER_URL: "revoked"}

    @pytest.mark.parametrize("withdrawn", [True, False])
    async def test_a_retry_after_convergence_writes_nothing(
        self, submission, held_twice, withdrawn
    ):
        assert await di.provision_user_shares(submission) is True
        held_twice.decide(OWN_URL, RESEARCH, granted=False)
        if not withdrawn:
            held_twice.decide(HOLDER_URL, RESEARCH, granted=False)
            held_twice.decide(
                HOLDER_URL,
                RESEARCH,
                granted=True,
                keys=[f"pod:{POD}"],
                legal_basis=dict(RELAYED_EVIDENCE),
            )
        assert await di.provision_user_shares(submission, raise_on_error=True) is True
        converged = {k: dict(v) for k, v in held_twice.rows.items()}
        held_twice.requests.clear()

        assert await di.provision_user_shares(submission, raise_on_error=True) is True
        assert await di.provision_user_shares(submission, raise_on_error=True) is True

        assert held_twice.posts() == []
        assert held_twice.rows == converged

    async def test_an_unreadable_connector_fails_the_retry_and_nothing_is_written(
        self, submission, held_twice
    ):
        assert await di.provision_user_shares(submission) is True
        held_twice.decide(OWN_URL, RESEARCH, granted=False)
        held_twice.refuse.clear()
        held_twice.unreadable[HOLDER_URL] = 503
        before = {k: dict(v) for k, v in held_twice.rows.items()}
        held_twice.requests.clear()

        with pytest.raises(ValueError, match="could not read what example-dso's connector"):
            await di.provision_user_shares(submission, raise_on_error=True)

        assert held_twice.posts() == []
        assert held_twice.rows == before
        assert submission.share_provisioned is False

    async def test_a_collectors_withdrawal_never_outranks_the_members_decision(
        self, submission, held_twice
    ):
        """Newer than the member's grant, and still not the member's decision.

        A membership that ended is withdrawn everywhere by revocation; one left at
        a single connector while the membership stands is not what the member
        decided, and the retry puts the member's decision back.
        """
        assert await di.provision_user_shares(submission) is True
        held_twice.decide(HOLDER_URL, RESEARCH, granted=False, decided_by="collector")

        assert await di.provision_user_shares(submission, raise_on_error=True) is True
        assert _research(held_twice) == {OWN_URL: "granted", HOLDER_URL: "granted"}

    async def test_a_members_withdrawal_after_a_collectors_one_stands(self, submission, held_twice):
        assert await di.provision_user_shares(submission) is True
        held_twice.decide(OWN_URL, RESEARCH, granted=False, decided_by="collector")
        held_twice.decide(HOLDER_URL, RESEARCH, granted=False)
        held_twice.requests.clear()

        assert await di.provision_user_shares(submission, raise_on_error=True) is True
        assert _research(held_twice) == {OWN_URL: "revoked", HOLDER_URL: "revoked"}
        assert held_twice.posts() == []


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
