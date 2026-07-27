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
def _no_provenance(monkeypatch):
    """Keep the emit out of the way unless a test asks for it."""
    import celine.onboarding.services.dataspace_identity as di

    async def _noop(**kw):
        return False

    monkeypatch.setattr(di, "emit_data_disclosed", _noop)


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
        return True

    monkeypatch.setattr(di, "emit_data_disclosed", _capture)

    await _export(tmp_path, [_sub()], purpose=["FlexibilityResearch"],
                  agreement_ref="dpa-1.0")

    assert captured["recipient_ref"] == "dso-org"
    assert captured["columns"] == ["pod_code"]
    assert captured["subject_count"] == 1
    assert captured["purpose"] == ["FlexibilityResearch"]
    assert captured["agreement_ref"] == "dpa-1.0"
    # A recomputable fingerprint of the consent state, not the consents.
    assert len(captured["consent_snapshot_hash"]) == 64


async def test_provenance_failure_does_not_fail_the_export(tmp_path, monkeypatch):
    """Accountability must never block the disclosure it documents.

    An operator who cannot export stops naming recipients, and the trail is lost
    entirely — the opposite of what the record exists for.
    """
    import celine.onboarding.services.dataspace_identity as di

    async def _boom(**kw):
        raise RuntimeError("provenance down")

    monkeypatch.setattr(di, "emit_data_disclosed", _boom)

    count, text = await _export(tmp_path, [_sub()])
    assert count == 1
    assert "IT001E00000001" in text
