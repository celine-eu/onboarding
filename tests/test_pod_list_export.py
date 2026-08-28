"""The POD list: the supply points a distributor may release, and nothing else.

The recipient wants PODs. Evidence, hashes and DIDs stay in the dataspace, where
they are verifiable and revocable — copying them into a second store is how two
records of the same consent start to disagree.

**Who is in the list comes from the connector, not from the intake form.** A
`Submission` is a form submitted once; it stops being true the moment the person
changes their mind in the participant webapp, and nothing writes back here. The
two failures that caused are asserted below: a grant made after intake was
ignored, and a withdrawal made after intake was not — the second being a
disclosure of personal data against a withdrawn consent.

Where no connector is configured there is no running system to ask, and the
local columns still decide. The file says which of the two wrote it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from celine.onboarding.outputs import csv_export

GENERATED_AT = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
OFFER = "household-energy-flexibility"
DATASET = "datasets.silver.meters_15m"
CONTROLLER = "grid-operator"
CONSUMER_DID = "did:web:grid-operator.dataspaces.localhost"

# The connector's published projection, as `GET /ns/sharing-offers` serves it.
# Coverage, resolution and retention are the offer's own terms — the same facts
# the person was shown — which is why the header takes them from here rather
# than from anything collected during intake.
OFFER_RECORD = {
    "id": OFFER,
    "purpose": "FlexibilityResearch",
    "requires_consent": True,
    "recipients": {
        "controller": CONTROLLER,
        "controller_role": "operations",
        "processors": {"category": "appointed-service-providers"},
    },
    "subject_scope": "own_data",
    "measures": ["consumption"],
    "resolution": "PT15M",
    "coverage": {"retrospective": "P1Y", "prospective": "P2Y"},
    "consent_text_version": "1.0",
    "revocable": True,
    "retention": "P2Y",
}


def _sub(**overrides):
    base = dict(
        ref="20260713-abcd",
        rec_slug="example",
        pod_code="IT001E00000001",
        dataspace_did="did:web:users.example:email-abc",
        dataspace_subject_id="email-abc",
        data_sharing_consent=True,
        data_sharing_consent_offer_ids=[OFFER],
        data_sharing_consent_text_version="1.0",
        share_provisioned=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Db:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _query):
        return _Result(self._rows)


@pytest.fixture(autouse=True)
def _disclosure_recorded(monkeypatch):
    """Stand in for the connector, which now records before the file is written.

    A success has to be the default: the call is fatal, so a stub that failed
    would stop every export in this module rather than let it be asserted.
    """
    import celine.onboarding.services.dataspace_identity as di

    async def _ok(**kw):
        return [
            {
                "dataset_id": DATASET,
                "consent_snapshot_hash": "a" * 64,
                "granted_party_count": 1,
            }
        ]

    monkeypatch.setattr(di, "record_disclosure", _ok)


@pytest.fixture()
def connector(monkeypatch):
    """The dataspace seam, stubbed at the two calls the export makes.

    Returns a setter for the audience so a test can say who currently consents
    without describing the connector's whole response shape each time. The HTTP
    behaviour of both calls is asserted in `test_dataspace_identity.py`; this
    fixture is about what the export does with their answers.
    """
    import celine.onboarding.services.dataspace_identity as di
    from celine.onboarding.services import template_service

    monkeypatch.setattr(csv_export.settings, "ds_connector_url", "http://connector")

    state: dict = {"subject_ids": set(), "dataset_id": DATASET, "offer": OFFER_RECORD}

    async def _offer(rec_slug, offer_id):
        if state["offer"] is None or state["offer"].get("id") != offer_id:
            raise ValueError(f"REC {rec_slug!r} publishes no sharing offer {offer_id!r}")
        return state["offer"]

    async def _did(alias):
        assert alias == CONTROLLER, "the recipient must come from the offer's controller"
        return CONSUMER_DID

    async def _audience(offer_id, consumer_id):
        assert consumer_id == CONSUMER_DID
        return di.OfferAudience(
            dataset_id=state["dataset_id"],
            subject_ids=frozenset(state["subject_ids"]),
            subject_count=len(state["subject_ids"]),
        )

    monkeypatch.setattr(template_service, "get_sharing_offer", _offer)
    monkeypatch.setattr(di, "resolve_consumer_did", _did)
    monkeypatch.setattr(di, "get_offer_audience", _audience)

    def _consents(*dids):
        state["subject_ids"] = set(dids)

    _consents.state = state
    return _consents


@pytest.fixture()
def no_connector(monkeypatch):
    """A deployment with no dataspace, where the intake form is the only record."""
    monkeypatch.setattr(csv_export.settings, "ds_connector_url", "")


async def _export(tmp_path, rows, **kw):
    out = tmp_path / "pods.csv"
    count = await csv_export.export_pod_list(
        _Db(rows),
        out,
        rec_slug="example",
        offer_id=OFFER,
        recipient_ref="dso-org",
        generated_at=GENERATED_AT,
        **kw,
    )
    return count, out.read_text(encoding="utf-8")


# ── the connector decides who is in the list ──────────────────────


async def test_a_grant_made_after_intake_is_honoured(tmp_path, connector):
    """Declined at intake, granted later in the webapp — they belong in the export.

    The local columns say no and the connector says yes. Reading the form left
    this person out and their consent unhonoured.
    """
    sub = _sub(data_sharing_consent=False, share_provisioned=False)
    connector(sub.dataspace_did)

    count, text = await _export(tmp_path, [sub])

    assert count == 1
    assert "IT001E00000001" in text


async def test_a_withdrawal_made_after_intake_is_honoured(tmp_path, connector):
    """Granted at intake, withdrawn later — they must not be in the export.

    This is the failure that mattered: the local columns still said granted, so
    the supply point kept going out to the recipient against a consent the
    person had withdrawn.
    """
    sub = _sub()
    connector()  # the connector reports nobody

    count, text = await _export(tmp_path, [sub])

    assert count == 0
    assert "IT001E00000001" not in text


async def test_the_local_offer_ids_do_not_widen_the_connector(tmp_path, connector):
    """A member the connector omits stays out however the form reads."""
    sub = _sub(data_sharing_consent_offer_ids=[OFFER, "grid-operations-planning"])
    connector("did:web:users.example:somebody-else")

    count, _ = await _export(tmp_path, [sub])
    assert count == 0


async def test_share_provisioned_is_not_consulted(tmp_path, connector):
    """This service's memory of its own call is not a reason to exclude anyone.

    `share_provisioned` records that provisioning succeeded here. The connector
    reporting the subject already accounts for whether the consent reached it,
    so a stale local flag must not drop a person whose consent is real.
    """
    sub = _sub(share_provisioned=False)
    connector(sub.dataspace_did)

    count, _ = await _export(tmp_path, [sub])
    assert count == 1


async def test_excludes_a_submission_without_a_pod(tmp_path, connector):
    sub = _sub(pod_code=None)
    connector(sub.dataspace_did)

    count, _ = await _export(tmp_path, [sub])
    assert count == 0


async def test_a_member_with_no_did_cannot_match(tmp_path, connector):
    """No DID, no join key — and no silent match on an empty value."""
    sub = _sub(dataspace_did=None)
    connector("")

    count, _ = await _export(tmp_path, [sub])
    assert count == 0


# ── what the file carries ─────────────────────────────────────────


async def test_writes_only_the_pod_column(tmp_path, connector):
    sub = _sub()
    connector(sub.dataspace_did)

    count, text = await _export(tmp_path, [sub])

    assert count == 1
    data_lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert data_lines == ["pod_code", "IT001E00000001"]


async def test_carries_no_personal_or_evidence_material(tmp_path, connector):
    """Everything the recipient did not ask for and must not receive."""
    sub = _sub()
    connector(sub.dataspace_did)

    _, text = await _export(tmp_path, [sub])

    for leaked in (
        sub.dataspace_did,
        sub.dataspace_subject_id,
        sub.ref,
        "1.0",  # consent text version
    ):
        assert leaked not in text


async def test_header_carries_the_offer_terms(tmp_path, connector):
    """Coverage, resolution and retention come from the offer, not from intake.

    They are properties of what was consented to, uniform across everyone who
    accepted it. Collecting them per person would be a second record of one
    fact — the thing this export is being repaired to stop doing.
    """
    sub = _sub()
    connector(sub.dataspace_did)

    _, text = await _export(tmp_path, [sub])

    assert "P1Y" in text and "P2Y" in text  # coverage, retrospective and prospective
    assert "PT15M" in text  # resolution
    assert "consumption" in text  # measures
    assert CONTROLLER in text and "operations" in text  # controller and its role


async def test_header_names_the_connector_and_keeps_the_staleness_promise(tmp_path, connector):
    """The promise that re-exporting bounds the staleness is only true here."""
    sub = _sub()
    connector(sub.dataspace_did)

    _, text = await _export(tmp_path, [sub])

    assert "2026-07-27T09:00:00+00:00" in text
    assert "connector" in text
    assert CONSUMER_DID in text
    assert "snapshot" in text
    assert "withdrawn" in text


# ── no connector: the intake form is the only record ──────────────


async def test_without_a_connector_the_local_columns_decide(tmp_path, no_connector):
    count, text = await _export(tmp_path, [_sub()])

    assert count == 1
    assert "IT001E00000001" in text


async def test_without_a_connector_a_different_offer_is_excluded(tmp_path, no_connector):
    """Consent is purpose-scoped: another offer is not this handover."""
    count, text = await _export(
        tmp_path, [_sub(data_sharing_consent_offer_ids=["grid-operations-planning"])]
    )

    assert count == 0
    assert "IT001E00000001" not in text


async def test_without_a_connector_the_header_does_not_promise_freshness(tmp_path, no_connector):
    """Re-exporting cannot bound a staleness that lives in the source.

    Against the local columns every re-export reproduces the same answer, so
    the header must not tell the recipient that a newer file is a fresher one.
    """
    _, text = await _export(tmp_path, [_sub()])

    assert "intake" in text
    assert "re-exporting will not pick it up" in text


# ── refusals ──────────────────────────────────────────────────────


async def test_an_offer_without_a_controller_is_refused(tmp_path, connector):
    """No controller, no recipient — and nothing to guess one from."""
    connector.state["offer"] = {**OFFER_RECORD, "recipients": {"processors": {}}}
    sub = _sub()
    connector(sub.dataspace_did)

    out = tmp_path / "pods.csv"
    with pytest.raises(ValueError, match="names no controller"):
        await csv_export.export_pod_list(
            _Db([sub]),
            out,
            rec_slug="example",
            offer_id=OFFER,
            recipient_ref="dso-org",
            generated_at=GENERATED_AT,
        )
    assert not out.exists()


# ── the disclosure record ─────────────────────────────────────────


async def test_records_the_disclosure(tmp_path, monkeypatch, connector):
    import celine.onboarding.services.dataspace_identity as di

    captured: dict = {}

    async def _capture(**kw):
        captured.update(kw)
        return [
            {
                "dataset_id": DATASET,
                "consent_snapshot_hash": "a" * 64,
                "granted_party_count": 1,
            }
        ]

    monkeypatch.setattr(di, "record_disclosure", _capture)

    sub = _sub()
    connector(sub.dataspace_did)
    await _export(tmp_path, [sub], purpose=["FlexibilityResearch"], agreement_ref="dpa-1.0")

    assert captured["recipient_ref"] == "dso-org"
    assert captured["columns"] == ["pod_code"]
    assert captured["subject_count"] == 1
    assert captured["purpose"] == ["FlexibilityResearch"]
    assert captured["agreement_ref"] == "dpa-1.0"
    # Named by offer. The connector resolves the datasets and computes the hash;
    # neither is something this service can honestly supply.
    assert captured["offer_id"] == OFFER
    # Stable across retries of this export, so a retry after a partial failure
    # re-records rather than duplicating.
    assert captured["event_id"].startswith("pod-list:example:")


async def test_the_purpose_defaults_to_the_offers(tmp_path, monkeypatch, connector):
    """The offer is the authority on its own purpose, as it is on its controller."""
    import celine.onboarding.services.dataspace_identity as di

    captured: dict = {}

    async def _capture(**kw):
        captured.update(kw)
        return [{"dataset_id": DATASET, "consent_snapshot_hash": "a" * 64}]

    monkeypatch.setattr(di, "record_disclosure", _capture)

    sub = _sub()
    connector(sub.dataspace_did)
    await _export(tmp_path, [sub])

    assert captured["purpose"] == ["FlexibilityResearch"]


async def test_the_disclosure_counts_the_file_not_the_audience(tmp_path, monkeypatch, connector):
    """`subject_count` describes the handover, so it counts rows that went out.

    A subject who consents but holds no supply point in this community is in
    the audience and not in the export, and the event must describe the second.
    """
    import celine.onboarding.services.dataspace_identity as di

    captured: dict = {}

    async def _capture(**kw):
        captured.update(kw)
        return [{"dataset_id": DATASET, "consent_snapshot_hash": "a" * 64}]

    monkeypatch.setattr(di, "record_disclosure", _capture)

    with_pod = _sub()
    without_pod = _sub(
        ref="20260713-efgh", dataspace_did="did:web:users.example:no-pod", pod_code=None
    )
    connector(with_pod.dataspace_did, without_pod.dataspace_did)

    count, _ = await _export(tmp_path, [with_pod, without_pod])

    assert count == 1
    assert captured["subject_count"] == 1


async def test_a_refused_disclosure_writes_no_file(tmp_path, monkeypatch, connector):
    """The reversal of the old policy, asserted.

    The emit used to run after the file was written and was non-fatal, so an
    export could go out with nothing describing it. The connector call runs
    first and a refusal must stop the handover.
    """
    import celine.onboarding.services.dataspace_identity as di

    async def _boom(**kw):
        raise RuntimeError("Disclosure was not recorded (502)")

    monkeypatch.setattr(di, "record_disclosure", _boom)

    sub = _sub()
    connector(sub.dataspace_did)

    out = tmp_path / "pods.csv"
    with pytest.raises(RuntimeError, match="not recorded"):
        await csv_export.export_pod_list(
            _Db([sub]),
            out,
            rec_slug="example",
            offer_id=OFFER,
            recipient_ref="dso-org",
            generated_at=GENERATED_AT,
        )

    assert not out.exists(), "a refused disclosure must leave no file behind"
