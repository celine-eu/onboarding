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


async def test_no_connector_configured_returns_empty(monkeypatch):
    monkeypatch.setattr(ts.settings, "ds_ns_url", "")
    monkeypatch.setattr(ts.settings, "ds_connector_url", "")
    monkeypatch.setattr(ts, "load_manifest", lambda slug: {"consent": {"data_sharing": {}}})
    assert await ts.get_sharing_offers("rec") == []


async def test_allow_list_filters(monkeypatch):
    monkeypatch.setattr(ts.settings, "ds_ns_url", "http://connector:30001")
    monkeypatch.setattr(
        ts, "load_manifest",
        lambda slug: {"consent": {"data_sharing": {"offers": ["consent-a"]}}},
    )
    _serve_offers(monkeypatch)
    offers = await ts.get_sharing_offers("rec")
    assert [o["id"] for o in offers] == ["consent-a"]


async def test_default_is_consent_based_only(monkeypatch):
    monkeypatch.setattr(ts.settings, "ds_ns_url", "http://connector:30001")
    monkeypatch.setattr(
        ts, "load_manifest",
        lambda slug: {"consent": {"data_sharing": {}}},  # no allow-list
    )
    _serve_offers(monkeypatch)
    offers = await ts.get_sharing_offers("rec")
    assert [o["id"] for o in offers] == ["consent-a", "consent-b"]


async def test_connector_unreachable_returns_empty(monkeypatch):
    monkeypatch.setattr(ts.settings, "ds_ns_url", "http://connector:30001")
    monkeypatch.setattr(ts, "load_manifest", lambda slug: {"consent": {"data_sharing": {}}})

    def boom(req):
        raise httpx.ConnectError("down")

    _patch_httpx(monkeypatch, boom)
    assert await ts.get_sharing_offers("rec") == []
