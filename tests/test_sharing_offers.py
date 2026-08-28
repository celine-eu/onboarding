"""Block B — the wizard's data-sharing offer resolution (§3.4 backend)."""

from __future__ import annotations

import httpx
import pytest

import celine.onboarding.services.template_service as ts

_OriginalAsyncClient = httpx.AsyncClient

_OFFERS = [
    {"id": "consent-a", "requires_consent": True},
    {"id": "consent-b", "requires_consent": True},
    {"id": "contract-c", "requires_consent": False},
]


def _patch_httpx(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def factory(**kw):
        kw.pop("transport", None)
        return _OriginalAsyncClient(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _serve_offers(monkeypatch):
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json=_OFFERS))


async def test_no_data_sharing_block_returns_empty(monkeypatch):
    monkeypatch.setattr(ts.settings, "ds_ns_url", "http://connector:30001")
    monkeypatch.setattr(ts, "load_manifest", lambda slug: {"consent": {}})
    _serve_offers(monkeypatch)
    assert await ts.get_sharing_offers("rec") == []


async def test_no_connector_configured_raises(monkeypatch):
    """Declaring data_sharing with no vocabulary is a misconfiguration, not "none".

    An empty list would be indistinguishable from a community that shares
    nothing, so the step would vanish and every consent in that window would be
    one nobody was asked for. Startup validation normally refuses this
    combination; reaching it means the configuration changed under a running
    process.
    """
    monkeypatch.setattr(ts.settings, "ds_ns_url", "")
    monkeypatch.setattr(ts.settings, "ds_connector_url", "")
    monkeypatch.setattr(ts, "load_manifest", lambda slug: {"consent": {"data_sharing": {}}})
    with pytest.raises(ts.SharingOffersUnavailableError):
        await ts.get_sharing_offers("rec")


async def test_allow_list_filters(monkeypatch):
    monkeypatch.setattr(ts.settings, "ds_ns_url", "http://connector:30001")
    monkeypatch.setattr(
        ts,
        "load_manifest",
        lambda slug: {"consent": {"data_sharing": {"offers": ["consent-a"]}}},
    )
    _serve_offers(monkeypatch)
    offers = await ts.get_sharing_offers("rec")
    assert [o["id"] for o in offers] == ["consent-a"]


async def test_default_is_consent_based_only(monkeypatch):
    monkeypatch.setattr(ts.settings, "ds_ns_url", "http://connector:30001")
    monkeypatch.setattr(
        ts,
        "load_manifest",
        lambda slug: {"consent": {"data_sharing": {}}},  # no allow-list
    )
    _serve_offers(monkeypatch)
    offers = await ts.get_sharing_offers("rec")
    assert [o["id"] for o in offers] == ["consent-a", "consent-b"]


async def test_connector_unreachable_raises_rather_than_falling_back(monkeypatch):
    """Fail closed, and say so.

    Never render offers from a cached or local copy: the hash of what was shown
    only means something if the facts came from the published vocabulary. But an
    unreachable vocabulary must be reported, not swallowed — the API turns this
    into a 503 so the wizard can say the options are temporarily unavailable.
    """
    monkeypatch.setattr(ts.settings, "ds_ns_url", "http://connector:30001")
    monkeypatch.setattr(ts, "load_manifest", lambda slug: {"consent": {"data_sharing": {}}})

    def boom(req):
        raise httpx.ConnectError("down")

    _patch_httpx(monkeypatch, boom)
    with pytest.raises(ts.SharingOffersUnavailableError):
        await ts.get_sharing_offers("rec")


# ── one offer, for the export ─────────────────────────────────────


async def test_resolves_one_offer_by_id(monkeypatch):
    monkeypatch.setattr(ts.settings, "ds_ns_url", "http://connector:30001")
    monkeypatch.setattr(ts, "load_manifest", lambda slug: {"consent": {"data_sharing": {}}})
    _serve_offers(monkeypatch)

    offer = await ts.get_sharing_offer("rec", "consent-a")

    assert offer["id"] == "consent-a"


async def test_an_offer_this_rec_does_not_publish_is_refused(monkeypatch):
    """The allow-list decides both questions, because they are one question.

    An offer this community does not offer is not one it may export under, so
    the export resolves it through the same path the wizard renders from rather
    than filtering the connector's whole vocabulary.
    """
    monkeypatch.setattr(ts.settings, "ds_ns_url", "http://connector:30001")
    monkeypatch.setattr(
        ts, "load_manifest", lambda slug: {"consent": {"data_sharing": {"offers": ["consent-b"]}}}
    )
    _serve_offers(monkeypatch)

    with pytest.raises(ValueError, match="publishes no sharing offer"):
        await ts.get_sharing_offer("rec", "consent-a")


async def test_an_unreadable_vocabulary_fails_closed(monkeypatch):
    """Never export against a cached or local copy of the offer's terms."""
    monkeypatch.setattr(ts.settings, "ds_ns_url", "http://connector:30001")
    monkeypatch.setattr(ts, "load_manifest", lambda slug: {"consent": {"data_sharing": {}}})
    _patch_httpx(monkeypatch, lambda req: httpx.Response(503, text="unavailable"))

    with pytest.raises(ts.SharingOffersUnavailableError):
        await ts.get_sharing_offer("rec", "consent-a")
