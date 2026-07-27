"""Block C — a CSV export records a DataDisclosed provenance event.

The disclosure snapshot is offer-level (dataset-level resolution lives in the
connector) and carries codes, DIDs and hashes only — never PII. The emit is
non-fatal: a provenance failure must never fail the export it documents.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx

import celine.onboarding.services.dataspace_identity as di
from celine.onboarding.outputs import csv_export

from test_dataspace_identity import _mock_token_provider, _patch_httpx  # noqa: F401


def _sub(**overrides) -> SimpleNamespace:
    base = dict(
        dataspace_did="did:web:users.example:email-abc",
        dataspace_subject_id="email-abc",
        ref="20260713-abcd",
        rec_slug="example",
        data_sharing_consent=True,
        data_sharing_consent_offer_ids=["household-energy-flexibility"],
        data_sharing_consent_text_version="1.0",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── snapshot hash ─────────────────────────────────────────────────────────────

def test_snapshot_hash_order_independent_and_drops_non_consented():
    a = _sub(dataspace_did="did:web:a")
    b = _sub(dataspace_did="did:web:b")
    opted_out = _sub(dataspace_did="did:web:c", data_sharing_consent=False)

    h1 = csv_export._disclosure_snapshot_hash([a, b, opted_out])
    h2 = csv_export._disclosure_snapshot_hash([b, opted_out, a])
    assert h1 == h2
    assert len(h1) == 64
    # the opted-out submission contributes nothing
    assert csv_export._disclosure_snapshot_hash([a, b]) == h1


def test_snapshot_hash_reacts_to_text_version():
    base = csv_export._disclosure_snapshot_hash([_sub()])
    bumped = csv_export._disclosure_snapshot_hash(
        [_sub(data_sharing_consent_text_version="2.0")]
    )
    assert bumped != base


# ── emit_data_disclosed ───────────────────────────────────────────────────────

async def test_emit_disabled_without_provenance_url(monkeypatch):
    monkeypatch.setattr(di.settings, "ds_provenance_url", "")
    assert await di.emit_data_disclosed(recipient_ref="dso") is False


async def test_emit_posts_data_disclosed(monkeypatch, bind_rec):
    di._token_provider = None
    monkeypatch.setattr(di.settings, "ds_provenance_url", "http://prov:30000")
    # The disclosing agent is the REC that holds the data, so it comes from that
    # community's manifest binding — not a deployment-wide setting.
    bind_rec("example", organization="rec-example",
             organization_did="did:web:rec.example")
    di._token_provider = _mock_token_provider()

    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["auth"] = req.headers.get("authorization")
        captured["json"] = json.loads(req.content)
        return httpx.Response(201, json={"event_id": "e1", "status": "created"})

    _patch_httpx(monkeypatch, handler)

    ok = await di.emit_data_disclosed(
        recipient_ref="dso-org",
        purpose=["GridMonitoring"],
        columns=["pod_code", "consumption"],
        subject_count=3,
        source_ref="example",
        consent_snapshot_hash="deadbeef",
        agreement_ref="dpa-1.0",
        rec_slug="example",
    )
    assert ok is True
    assert captured["url"].endswith("/prov/events")
    assert captured["auth"] == "Bearer test-token"
    body = captured["json"]
    assert body["event_type"] == "DataDisclosed"
    assert body["recipient_ref"] == "dso-org"
    assert body["purpose"] == ["GridMonitoring"]
    assert body["columns"] == ["pod_code", "consumption"]
    assert body["disclosed_by"] == "did:web:rec.example"
    assert body["consent_snapshot_hash"] == "deadbeef"
    assert body["agreement_ref"] == "dpa-1.0"


async def test_emit_is_non_fatal_on_http_error(monkeypatch):
    di._token_provider = _mock_token_provider()
    monkeypatch.setattr(di.settings, "ds_provenance_url", "http://prov:30000")
    _patch_httpx(monkeypatch, lambda req: httpx.Response(500, text="boom"))
    # Must not raise; returns False so the export it documents still succeeds.
    assert await di.emit_data_disclosed(recipient_ref="dso") is False
