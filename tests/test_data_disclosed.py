"""Block C — a POD-list export records its disclosure with the connector.

Rewritten for `POST /admin/disclosure`. The previous version asserted a direct
`POST {DS_PROVENANCE_URL}/prov/events` carrying a locally-computed
`consent_snapshot_hash`. That call is gone, and so is the hash:

- provenance made `dataset_id` required, so every emit was answered 422 and
  discarded — silently, because the emit was non-fatal;
- the hash a discloser can compute is over its *own* submissions, and the one
  `L-2` asks for is over the connector's granted consent rows. Two digests over
  two tables. The connector computes it now and returns it.

The call is also **named by offer**. A POD list is scoped to one sharing offer;
the connector resolves it to the datasets it reaches and records one event per
dataset.
"""

from __future__ import annotations

import json

import httpx
import pytest
from test_dataspace_identity import _mock_token_provider, _patch_httpx  # noqa: F401

import celine.onboarding.services.dataspace_identity as di


def _ok(payload=None):
    body = payload or {
        "status": "recorded",
        "offer_id": "household-energy-flexibility",
        "disclosures": [
            {
                "dataset_id": "datasets.silver.meters_15m",
                "consent_snapshot_hash": "a" * 64,
                "granted_party_count": 3,
            }
        ],
    }
    return lambda req: httpx.Response(200, json=body)


async def test_no_connector_configured_is_fatal(monkeypatch):
    """Not silently skipped.

    The old emit returned False and let the export proceed. An unrecorded
    handover is precisely what this call exists to prevent, so a missing
    connector must stop it.
    """
    monkeypatch.setattr(di.settings, "ds_connector_url", "")
    with pytest.raises(RuntimeError, match="DS_CONNECTOR_URL"):
        await di.record_disclosure(offer_id="o", recipient_ref="dso")


async def test_posts_the_offer_and_lets_the_connector_expand_it(monkeypatch, bind_rec):
    di._token_provider = None
    monkeypatch.setattr(di.settings, "ds_connector_url", "http://connector:30001")
    # The disclosing agent is the REC that holds the data, so it comes from that
    # community's manifest binding — not a deployment-wide setting.
    bind_rec("example", organization="rec-example", organization_did="did:web:rec.example")
    di._token_provider = _mock_token_provider()

    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["auth"] = req.headers.get("authorization")
        captured["json"] = json.loads(req.content)
        return _ok()(req)

    _patch_httpx(monkeypatch, handler)

    disclosures = await di.record_disclosure(
        offer_id="household-energy-flexibility",
        recipient_ref="dso-org",
        purpose=["GridMonitoring"],
        columns=["pod_code"],
        subject_count=3,
        source_ref="example",
        agreement_ref="dpa-1.0",
        event_id="pod-list:example:household-energy-flexibility:2026-08-27",
        rec_slug="example",
    )

    assert captured["url"].endswith("/admin/disclosure")
    assert captured["auth"] == "Bearer test-token"
    body = captured["json"]
    assert body["offer_id"] == "household-energy-flexibility"
    assert body["recipient_ref"] == "dso-org"
    assert body["columns"] == ["pod_code"]
    assert body["disclosed_by"] == "did:web:rec.example"
    assert body["agreement_ref"] == "dpa-1.0"
    # The two fields the caller cannot honestly supply are not sent.
    assert "dataset_id" not in body
    assert "consent_snapshot_hash" not in body

    assert disclosures[0]["dataset_id"] == "datasets.silver.meters_15m"
    assert disclosures[0]["consent_snapshot_hash"] == "a" * 64


async def test_every_dataset_is_returned_not_just_the_first(monkeypatch, bind_rec):
    """An offer may reach several datasets, and today's fixture reaching one is
    a property of the fixture. A caller reading `disclosures[0]` would be right
    now and wrong the day a second dataset declares the offer."""
    di._token_provider = _mock_token_provider()
    monkeypatch.setattr(di.settings, "ds_connector_url", "http://connector:30001")
    bind_rec("example", organization="rec-example")
    _patch_httpx(
        monkeypatch,
        _ok(
            {
                "status": "recorded",
                "offer_id": "o",
                "disclosures": [
                    {
                        "dataset_id": "d1",
                        "consent_snapshot_hash": "b" * 64,
                        "granted_party_count": 2,
                    },
                    {
                        "dataset_id": "d2",
                        "consent_snapshot_hash": "c" * 64,
                        "granted_party_count": 5,
                    },
                ],
            }
        ),
    )

    disclosures = await di.record_disclosure(offer_id="o", recipient_ref="dso", rec_slug="example")
    assert [d["dataset_id"] for d in disclosures] == ["d1", "d2"]


async def test_a_refusal_is_fatal(monkeypatch, bind_rec):
    """The opposite of the old policy, and the change is the point.

    The old emit documented something that had already happened, so losing it
    was worse than failing. This runs *before* the handover, so a refusal means
    the disclosure does not happen.
    """
    di._token_provider = _mock_token_provider()
    monkeypatch.setattr(di.settings, "ds_connector_url", "http://connector:30001")
    bind_rec("example", organization="rec-example")
    _patch_httpx(monkeypatch, lambda req: httpx.Response(502, text="partial"))

    with pytest.raises(RuntimeError, match="must not be handed over"):
        await di.record_disclosure(offer_id="o", recipient_ref="dso", rec_slug="example")


async def test_an_empty_expansion_is_refused(monkeypatch, bind_rec):
    """A 200 describing nothing is not a recorded disclosure."""
    di._token_provider = _mock_token_provider()
    monkeypatch.setattr(di.settings, "ds_connector_url", "http://connector:30001")
    bind_rec("example", organization="rec-example")
    _patch_httpx(monkeypatch, _ok({"status": "recorded", "offer_id": "o", "disclosures": []}))

    with pytest.raises(RuntimeError, match="recorded no disclosure"):
        await di.record_disclosure(offer_id="o", recipient_ref="dso", rec_slug="example")
