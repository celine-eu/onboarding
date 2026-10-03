"""Consent documents resolved from the legal host, with the manifest winning and a fallback.

`services/legal_documents.py`. The host's `current.json` is faked with httpx's
MockTransport; nothing here touches a network.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from celine.onboarding.config.settings import settings
from celine.onboarding.models.schemas import ConsentCreate
from celine.onboarding.services import legal_documents as ld
from celine.onboarding.services import submission_service, template_service

BASE = "http://legal.example.org"
CURRENT = {
    "owner": "rec-a",
    "env": "local",
    "slots": {
        "privacy": {"id": "rec-a/privacy-notice", "version": "0.3", "status": "final",
                    "paths": {"it": "/rec-a/privacy-notice/0.3/it/", "en": "/rec-a/privacy-notice/0.3/en/"},
                    "sha256": {"it": "a" * 64, "en": "b" * 64}, "titles": {"it": "Informativa"}},
        "regulations": {"id": "rec-a/rules", "external": True, "version": "2026-06-13",
                        "url": "https://rec-a.example.org/rules.pdf", "sha256": "c" * 64},
        "statute": {"id": "rec-a/statute", "external": True, "version": "2024-05", "url": None, "sha256": "d" * 64},
        "data_sharing_notice": {"id": "rec-a/ds-notice", "version": "0.2", "status": "final",
                                "paths": {"it": "/rec-a/ds-notice/0.2/it/"}, "sha256": {"it": "e" * 64}},
    },
}
ASK = {"required": True}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(ld, "_current", {})
    monkeypatch.setattr(ld, "_fetched_at", {})
    monkeypatch.setattr(settings, "legal_base_url", BASE)


def _manifest(**consent):
    return {"slug": "rec-a", "locale": "it", "rec_registry": {"community": "rec-a"}, "consent": consent}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _serves(body, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        return httpx.Response(200, json=body)
    return handler


def test_without_a_legal_host_the_manifest_is_as_it_was(monkeypatch):
    monkeypatch.setattr(settings, "legal_base_url", "")
    manifest = _manifest(gdpr=ASK, statute={"version": "1.0", "file": "consent/statute.pdf"})
    assert ld.consent_documents("rec-a", manifest) == {"gdpr": ASK, "statute": manifest["consent"]["statute"]}
    assert ld.data_sharing_notice("rec-a", _manifest(data_sharing={})) is None


def test_the_manifests_own_link_wins():
    ld._current["rec-a"] = CURRENT
    own = {"version": "9", "url": "https://elsewhere.example.org/privacy"}
    assert ld.consent_documents("rec-a", _manifest(gdpr=own))["gdpr"] == own


async def test_from_the_host_the_versioned_page_in_the_communitys_language():
    manifest = _manifest(gdpr=ASK, policy=ASK, statute=ASK)
    await ld.refresh("rec-a", manifest, _client(_serves(CURRENT)))
    documents = ld.consent_documents("rec-a", manifest)
    assert documents["gdpr"] == {"required": True, "url": f"{BASE}/rec-a/privacy-notice/0.3/it/",
                                 "version": "0.3", "sha256": "a" * 64}
    assert documents["policy"]["url"] == "https://rec-a.example.org/rules.pdf"
    assert documents["policy"]["version"] == "2026-06-13"
    assert "statute" not in documents  # published nowhere: not asked


async def test_a_slot_the_host_does_not_have_is_not_asked():
    body = {**CURRENT, "slots": {k: v for k, v in CURRENT["slots"].items() if k != "regulations"}}
    manifest = _manifest(gdpr=ASK, policy=ASK)
    await ld.refresh("rec-a", manifest, _client(_serves(body)))
    assert list(ld.consent_documents("rec-a", manifest)) == ["gdpr"]


def test_before_the_host_answers_the_slot_address_and_no_version():
    documents = ld.consent_documents("rec-a", _manifest(gdpr=ASK))
    assert documents["gdpr"] == {"required": True, "url": f"{BASE}/rec-a/privacy/", "version": None}


async def test_a_host_that_stops_answering_keeps_the_last_good_copy(monkeypatch):
    manifest = _manifest(gdpr=ASK)
    await ld.refresh("rec-a", manifest, _client(_serves(CURRENT)))
    monkeypatch.setattr(ld, "_fetched_at", {})  # due again
    await ld.refresh("rec-a", manifest, _client(lambda r: httpx.Response(503)))
    assert ld.consent_documents("rec-a", manifest)["gdpr"]["version"] == "0.3"


async def test_it_is_fetched_once_per_interval_and_from_the_communitys_address():
    calls: list[str] = []
    manifest = _manifest(gdpr=ASK)
    for _ in range(3):
        await ld.refresh("rec-a", manifest, _client(_serves(CURRENT, calls)))
    assert calls == [f"{BASE}/rec-a/current.json"]


def test_the_notice_comes_from_the_manifest_or_the_host():
    ld._current["rec-a"] = CURRENT
    assert ld.data_sharing_notice("rec-a", _manifest(data_sharing={"notice_url": "https://x/n"}))["url"] == "https://x/n"
    notice = ld.data_sharing_notice("rec-a", _manifest(data_sharing={}))
    assert notice["url"] == f"{BASE}/rec-a/ds-notice/0.2/it/" and notice["version"] == "0.2"
    assert ld.data_sharing_notice("rec-a", _manifest()) is None  # no data-sharing step


def test_the_config_carries_the_resolved_documents_and_the_notice(seed_rec):
    ld._current["rec-a"] = CURRENT
    seed_rec("rec-a", locale="it", rec_registry={"community": "rec-a"},
             consent={"gdpr": ASK, "statute": ASK, "data_sharing": {"required": False}})
    consent = template_service.get_config("rec-a")["consent"]
    assert consent["gdpr"]["url"] == f"{BASE}/rec-a/privacy-notice/0.3/it/"
    assert "statute" not in consent
    assert consent["data_sharing"]["notice"]["version"] == "0.2"
    assert template_service.consent_slots("rec-a") == ("gdpr",)


async def _create(data: ConsentCreate):
    db = MagicMock()
    db.commit, db.refresh = AsyncMock(), AsyncMock()
    await submission_service.create_from_consent(db, data, "198.51.100.20", "rec-a")
    return db.add.call_args.args[0]


async def test_the_recorded_version_is_the_served_one(seed_rec):
    ld._current["rec-a"] = CURRENT
    seed_rec("rec-a", locale="it", rec_registry={"community": "rec-a"}, consent={"gdpr": ASK})
    row = await _create(ConsentCreate(gdpr_consent=True, gdpr_consent_version="0.3"))
    assert row.gdpr_consent_version == "0.3"
    row = await _create(ConsentCreate(gdpr_consent=True))  # an old client sending no version
    assert row.gdpr_consent_version == "0.3"


async def test_a_stale_page_is_refused(seed_rec):
    ld._current["rec-a"] = CURRENT
    seed_rec("rec-a", locale="it", rec_registry={"community": "rec-a"}, consent={"gdpr": ASK})
    with pytest.raises(submission_service.ConsentVersionMismatchError, match="0.3"):
        await _create(ConsentCreate(gdpr_consent=True, gdpr_consent_version="0.2"))


async def test_an_unknown_version_is_recorded_without_one(seed_rec):
    seed_rec("rec-a", locale="it", rec_registry={"community": "rec-a"}, consent={"gdpr": ASK})
    row = await _create(ConsentCreate(gdpr_consent=True, gdpr_consent_version=None))
    assert row.gdpr_consent and row.gdpr_consent_version is None and row.gdpr_consent_at is not None
