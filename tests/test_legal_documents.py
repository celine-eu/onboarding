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


def _template_with(monkeypatch, tmp_path, *slots: str) -> None:
    """A template directory for `rec-a` keeping these slots' documents, as served."""
    consent = tmp_path / "rec-a" / "consent"
    consent.mkdir(parents=True)
    for slot in slots:
        (consent / f"{slot}.pdf").write_bytes(b"%PDF-1.4")
        (consent / f"{slot}.pdf.json").write_text(json.dumps({"filename": f"{slot}.pdf"}))
    monkeypatch.setattr(settings, "templates_dir", str(tmp_path))


def test_without_a_legal_host_the_manifest_is_as_it_was(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "legal_base_url", "")
    _template_with(monkeypatch, tmp_path, "gdpr", "statute")
    manifest = _manifest(gdpr=ASK, statute={"version": "1.0", "file": "consent/statute.pdf"})
    assert ld.consent_documents("rec-a", manifest) == {"gdpr": ASK, "statute": manifest["consent"]["statute"]}
    assert ld.data_sharing_notice("rec-a", _manifest(data_sharing={})) is None


def test_without_a_legal_host_a_slot_with_no_document_is_not_asked(monkeypatch, tmp_path):
    """No `url`, nothing kept with the template, no host: a required box nobody could read."""
    monkeypatch.setattr(settings, "legal_base_url", "")
    _template_with(monkeypatch, tmp_path, "gdpr")
    own = {"version": "2026-06-13", "url": "https://rec-a.example.org/rules.pdf"}
    manifest = _manifest(gdpr=ASK, policy=own, statute={"version": "1.0", "required": True})
    assert ld.consent_documents("rec-a", manifest) == {"gdpr": ASK, "policy": own}


def test_a_file_the_template_does_not_have_is_not_asked(monkeypatch, tmp_path):
    _template_with(monkeypatch, tmp_path)
    ld._current["rec-a"] = CURRENT
    manifest = _manifest(statute={"version": "1.0", "file": "consent/statute.pdf"})
    assert ld.consent_documents("rec-a", manifest) == {}


def test_the_asked_slots_follow_the_documents(monkeypatch, tmp_path, seed_rec):
    """The wizard's boxes and the server's required check read the same resolution."""
    monkeypatch.setattr(settings, "legal_base_url", "")
    _template_with(monkeypatch, tmp_path, "gdpr", "policy")
    seed_rec("rec-a", consent={"gdpr": ASK, "policy": ASK, "statute": ASK})
    assert template_service.consent_slots("rec-a") == ("gdpr", "policy")
    assert "statute" not in template_service.get_config("rec-a")["consent"]


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
    # The page shown and its hash, as published: exactly which text was accepted.
    assert row.gdpr_consent_url == f"{BASE}/rec-a/privacy-notice/0.3/it/"
    assert row.gdpr_consent_sha256 == "a" * 64
    assert row.statute_consent_url is None and row.statute_consent_sha256 is None
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


def test_the_template_serves_only_its_own_documents(monkeypatch, tmp_path):
    _template_with(monkeypatch, tmp_path, "gdpr")
    consent = tmp_path / "rec-a" / "consent"
    served = template_service.consent_document_path("rec-a", "gdpr")
    assert served == (consent / "gdpr.pdf").resolve()
    assert template_service.consent_document_path("rec-a", "statute") is None
    # metadata, and no file behind it
    (consent / "policy.pdf.json").write_text(json.dumps({"filename": "policy.pdf"}))
    assert template_service.consent_document_path("rec-a", "policy") is None
    (consent / "terms.pdf.json").write_text(json.dumps({"filename": "../../outside.pdf"}))
    assert template_service.consent_document_path("rec-a", "terms") is None


# --- the terms, and the specific approval of their clauses ----------------------------

DECLARATIONS = {
    "it": {"general": "Dichiaro di accettare le Condizioni d'uso, versione 0.2.",
           "specific_approval": {
               "text": "Ai sensi degli artt. 1341 e 1342 c.c., approvo gli artt. 5 e 7.",
               "articles": [5, 7]}},
    "en": {"general": "I accept the Terms of use, version 0.2."},
}
TERMS = {"id": "rec-a/terms-of-use", "version": "0.2", "status": "final",
         "paths": {"it": "/rec-a/terms-of-use/0.2/it/", "en": "/rec-a/terms-of-use/0.2/en/"},
         "sha256": {"it": "f" * 64, "en": "9" * 64}, "titles": {"it": "Condizioni d'uso"},
         "declarations": DECLARATIONS}
WITH_TERMS = {**CURRENT, "slots": {**CURRENT["slots"], "terms": TERMS}}


def test_the_terms_resolve_on_the_host_with_the_declarations_of_the_language_shown():
    """
    @verifies REQ-0047
    """
    ld._current["rec-a"] = WITH_TERMS
    documents = ld.consent_documents("rec-a", _manifest(terms=ASK))
    assert documents["terms"] == {"required": True, "url": f"{BASE}/rec-a/terms-of-use/0.2/it/",
                                  "version": "0.2", "sha256": "f" * 64,
                                  "declarations": DECLARATIONS["it"]}


def test_the_clauses_are_the_terms_document_with_their_own_slot():
    """
    @verifies REQ-0047
    """
    ld._current["rec-a"] = WITH_TERMS
    documents = ld.consent_documents("rec-a", _manifest(terms=ASK, terms_clauses=ASK))
    clauses = documents["terms_clauses"]
    assert {k: clauses[k] for k in ("url", "version", "sha256")} == {
        k: documents["terms"][k] for k in ("url", "version", "sha256")}
    assert clauses["declarations"]["specific_approval"]["text"].startswith("Ai sensi")
    assert list(documents) == ["terms", "terms_clauses"]


def test_no_words_no_box_for_the_clauses():
    """A text without `specific_approval` in the language shown: the box is not asked.

    @verifies REQ-0047
    """
    ld._current["rec-a"] = WITH_TERMS
    english = {**_manifest(terms=ASK, terms_clauses=ASK), "locale": "en"}
    assert list(ld.consent_documents("rec-a", english)) == ["terms"]
    no_words = {**TERMS, "declarations": None}
    without = {**WITH_TERMS, "slots": {**CURRENT["slots"], "terms": no_words}}
    ld._current["rec-a"] = without
    assert list(ld.consent_documents("rec-a", _manifest(terms=ASK, terms_clauses=ASK))) == ["terms"]


def test_the_clauses_are_not_asked_without_the_terms():
    """
    @verifies REQ-0047
    """
    ld._current["rec-a"] = WITH_TERMS
    assert ld.consent_documents("rec-a", _manifest(terms_clauses=ASK)) == {}
    # The host has no terms for this community: neither box.
    ld._current["rec-a"] = CURRENT
    assert ld.consent_documents("rec-a", _manifest(terms=ASK, terms_clauses=ASK)) == {}


def test_before_the_host_answers_the_terms_are_asked_and_the_clauses_are_not():
    """The slot's address and no version, like every slot; but no words for box 2.

    @verifies REQ-0047
    """
    documents = ld.consent_documents("rec-a", _manifest(terms=ASK, terms_clauses=ASK))
    address = f"{BASE}/rec-a/terms/"
    assert documents == {"terms": {"required": True, "url": address, "version": None}}


def test_a_manifest_link_for_the_terms_asks_no_clauses():
    """Onboarding cannot read the operator's page, so it has no words for box 2.

    @verifies REQ-0047
    """
    ld._current["rec-a"] = WITH_TERMS
    own = {"version": "3", "url": "https://elsewhere.example.org/terms"}
    assert list(ld.consent_documents("rec-a", _manifest(terms=own, terms_clauses=ASK))) == ["terms"]


def test_without_a_legal_host_neither_is_asked(monkeypatch):
    """
    @verifies REQ-0047
    """
    monkeypatch.setattr(settings, "legal_base_url", "")
    assert ld.consent_documents("rec-a", _manifest(terms=ASK, terms_clauses=ASK)) == {}


async def test_both_are_recorded_each_on_its_own(seed_rec):
    """
    @verifies REQ-0047
    """
    ld._current["rec-a"] = WITH_TERMS
    seed_rec("rec-a", locale="it", rec_registry={"community": "rec-a"},
             consent={"terms": ASK, "terms_clauses": ASK})
    row = await _create(ConsentCreate(
        terms_consent=True, terms_consent_version="0.2",
        terms_clauses_consent=True, terms_clauses_consent_version="0.2",
    ))
    for slot in ("terms", "terms_clauses"):
        assert getattr(row, f"{slot}_consent") is True
        assert getattr(row, f"{slot}_consent_at") is not None
        assert getattr(row, f"{slot}_consent_version") == "0.2"
        assert getattr(row, f"{slot}_consent_url") == f"{BASE}/rec-a/terms-of-use/0.2/it/"
        assert getattr(row, f"{slot}_consent_sha256") == "f" * 64


async def test_the_clauses_are_required_and_never_implied_by_the_terms(seed_rec):
    """
    @verifies REQ-0047
    """
    ld._current["rec-a"] = WITH_TERMS
    seed_rec("rec-a", locale="it", rec_registry={"community": "rec-a"},
             consent={"terms": ASK, "terms_clauses": ASK})
    with pytest.raises(submission_service.ConsentNotGivenError, match="terms_clauses"):
        await _create(ConsentCreate(terms_consent=True, terms_consent_version="0.2"))
    with pytest.raises(submission_service.ConsentNotGivenError, match="terms"):
        await _create(ConsentCreate(terms_clauses_consent=True))


async def test_a_stale_clauses_box_is_refused(seed_rec):
    """
    @verifies REQ-0047
    """
    ld._current["rec-a"] = WITH_TERMS
    seed_rec("rec-a", locale="it", rec_registry={"community": "rec-a"},
             consent={"terms": ASK, "terms_clauses": ASK})
    with pytest.raises(submission_service.ConsentVersionMismatchError, match="terms_clauses"):
        await _create(ConsentCreate(
            terms_consent=True, terms_consent_version="0.2",
            terms_clauses_consent=True, terms_clauses_consent_version="0.1",
        ))


async def test_clauses_sent_where_they_are_not_asked_are_not_recorded(seed_rec):
    """
    @verifies REQ-0047
    """
    ld._current["rec-a"] = WITH_TERMS
    seed_rec("rec-a", locale="en", rec_registry={"community": "rec-a"},
             consent={"terms": ASK, "terms_clauses": ASK})
    row = await _create(ConsentCreate(terms_consent=True, terms_clauses_consent=True))
    assert row.terms_consent is True
    assert row.terms_clauses_consent is False and row.terms_clauses_consent_at is None


def test_the_config_carries_both_boxes_words(seed_rec):
    """
    @verifies REQ-0047
    """
    ld._current["rec-a"] = WITH_TERMS
    seed_rec("rec-a", locale="it", rec_registry={"community": "rec-a"},
             consent={"terms": ASK, "terms_clauses": ASK})
    consent = template_service.get_config("rec-a")["consent"]
    assert consent["terms"]["declarations"]["general"] == DECLARATIONS["it"]["general"]
    approval = consent["terms_clauses"]["declarations"]["specific_approval"]
    assert approval == DECLARATIONS["it"]["specific_approval"]
    assert template_service.consent_slots("rec-a") == ("terms", "terms_clauses")


@pytest.mark.parametrize("consent", [{"terms_clauses": ASK}, {"terms": "yes", "terms_clauses": ASK},
                                     {"terms": ASK, "terms_clauses": True}])
def test_a_manifest_with_clauses_and_no_terms_is_refused_at_load(consent):
    """
    @verifies REQ-0048
    """
    with pytest.raises(ValueError, match="terms_clauses"):
        template_service.validate_consent_slots(consent, where="REC 'rec-a'")


@pytest.mark.parametrize("consent", [None, {}, {"gdpr": ASK}, {"terms": ASK},
                                     {"terms": ASK, "terms_clauses": ASK}])
def test_a_manifest_with_both_or_neither_loads(consent):
    """
    @verifies REQ-0048
    """
    template_service.validate_consent_slots(consent, where="REC 'rec-a'")
