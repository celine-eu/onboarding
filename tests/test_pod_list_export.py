"""The POD list: the supply points a distributor may release, and nothing else.

The recipient wants PODs. Evidence, hashes and DIDs stay in the dataspace, where
they are verifiable and revocable — copying them into a second store is how two
records of the same consent start to disagree.

The file is a snapshot, so the re-export cadence is the revocation latency. It
says so in its own header.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from celine.onboarding.outputs import csv_export

GENERATED_AT = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
OFFER = "household-energy-flexibility"


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
        return [{
            "dataset_id": "datasets.silver.meters_15m",
            "consent_snapshot_hash": "a" * 64,
            "granted_party_count": 1,
        }]

    monkeypatch.setattr(di, "record_disclosure", _ok)


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


async def test_writes_only_the_pod_column(tmp_path):
    count, text = await _export(tmp_path, [_sub()])

    assert count == 1
    data_lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert data_lines == ["pod_code", "IT001E00000001"]


async def test_carries_no_personal_or_evidence_material(tmp_path):
    """Everything the recipient did not ask for and must not receive."""
    sub = _sub()
    _, text = await _export(tmp_path, [sub])

    for leaked in (
        sub.dataspace_did,
        sub.dataspace_subject_id,
        sub.ref,
        "1.0",  # consent text version
    ):
        assert leaked not in text


async def test_header_states_when_it_was_made_and_that_it_goes_stale(tmp_path):
    _, text = await _export(tmp_path, [_sub()])

    assert "2026-07-27T09:00:00+00:00" in text
    assert "snapshot" in text
    assert "withdrawn" in text


async def test_excludes_a_consent_for_a_different_offer(tmp_path):
    """Consent is purpose-scoped: another offer is not this handover."""
    count, text = await _export(
        tmp_path, [_sub(data_sharing_consent_offer_ids=["grid-operations-planning"])]
    )

    assert count == 0
    assert "IT001E00000001" not in text


async def test_excludes_a_submission_without_a_pod(tmp_path):
    count, _ = await _export(tmp_path, [_sub(pod_code=None)])
    assert count == 0


async def test_records_the_disclosure(tmp_path, monkeypatch):
    import celine.onboarding.services.dataspace_identity as di

    captured: dict = {}

    async def _capture(**kw):
        captured.update(kw)
        return [{
            "dataset_id": "datasets.silver.meters_15m",
            "consent_snapshot_hash": "a" * 64,
            "granted_party_count": 1,
        }]

    monkeypatch.setattr(di, "record_disclosure", _capture)

    await _export(tmp_path, [_sub()], purpose=["FlexibilityResearch"],
                  agreement_ref="dpa-1.0")

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


async def test_a_refused_disclosure_writes_no_file(tmp_path, monkeypatch):
    """The reversal of the old policy, asserted.

    The emit used to run after the file was written and was non-fatal, so an
    export could go out with nothing describing it. The connector call runs
    first and a refusal must stop the handover.
    """
    import celine.onboarding.services.dataspace_identity as di

    async def _boom(**kw):
        raise RuntimeError("Disclosure was not recorded (502)")

    monkeypatch.setattr(di, "record_disclosure", _boom)

    out = tmp_path / "pods.csv"
    with pytest.raises(RuntimeError, match="not recorded"):
        await csv_export.export_pod_list(
            _Db([_sub()]),
            out,
            rec_slug="example",
            offer_id=OFFER,
            recipient_ref="dso-org",
            generated_at=GENERATED_AT,
        )

    assert not out.exists(), "a refused disclosure must leave no file behind"
