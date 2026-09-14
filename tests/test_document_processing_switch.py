"""Document upload and scanning are a feature switch, not a condition for starting.

Scanning sends a participant's bill and identity document to the extraction
endpoint. Without the operator switching it on (`EXTRACTION_ENABLED`) and a key to
call it with (`EXTRACTION_API_KEY`), the feature is off: the service still starts and onboards
people from the fields they type, `/config` tells the wizard so, and the API
refuses every route that would store a document for scanning or read one.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import celine.onboarding.main as app_main
from celine.onboarding.api import deps
from celine.onboarding.config.settings import settings
from celine.onboarding.models.database import get_db


@pytest.fixture()
def switch(monkeypatch):
    """Set the two inputs of the switch; returns a setter."""

    def _set(*, dpa: bool, key: str):
        monkeypatch.setattr(settings, "extraction_enabled", dpa)
        monkeypatch.setattr(settings, "extraction_api_key", key)
        monkeypatch.setattr(settings, "removed_dpa_signed", "")
        monkeypatch.setattr(settings, "removed_openai_api_key", "")

    return _set


@pytest.fixture()
def client(bind_rec):
    from celine.onboarding.api.config import router as config_router
    from celine.onboarding.api.documents import router as documents_router
    from celine.onboarding.api.extractions import router as extractions_router

    bind_rec("rec-a")
    app = FastAPI()
    app.state.limiter = deps.limiter
    for router in (config_router, documents_router, extractions_router):
        app.include_router(router, prefix="/api/{rec_slug}")

    async def _no_db():
        yield None

    async def _a_session():
        return object()

    app.dependency_overrides[get_db] = _no_db
    app.dependency_overrides[deps.require_session] = _a_session
    return TestClient(app)


FILE = {"files": ("bill.pdf", b"%PDF-1.4", "application/pdf")}
GATED = [
    ("post", "/api/rec-a/extract", {"files": FILE}),
    ("post", "/api/rec-a/extract-id", {"files": FILE}),
    ("post", f"/api/rec-a/documents/{uuid.uuid4()}/extract", {}),
    ("post", f"/api/rec-a/extractions/{uuid.uuid4()}/confirm", {"json": {"extracted_data": {}}}),
    (
        "post",
        f"/api/rec-a/submissions/{uuid.uuid4()}/documents?doc_type=utility_bill",
        {"files": {"file": FILE["files"]}},
    ),
    (
        "post",
        f"/api/rec-a/submissions/{uuid.uuid4()}/documents?doc_type=id_card",
        {"files": {"file": FILE["files"]}},
    ),
]


# ── the switch ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("dpa", "key", "enabled"),
    [(True, "sk-test", True), (False, "sk-test", False), (True, "", False), (False, "", False)],
)
def test_it_needs_both_the_agreement_and_a_key(switch, dpa, key, enabled):
    switch(dpa=dpa, key=key)
    assert settings.document_processing_enabled is enabled


# ── startup ───────────────────────────────────────────────────────────────────


async def test_the_service_starts_with_the_switch_off(
    switch, bind_rec, monkeypatch, tmp_path, caplog
):
    """A REC collecting personal data used to be enough to refuse to boot."""
    from celine.onboarding.services import template_service

    switch(dpa=False, key="")
    bind_rec("rec-a")["steps"] = ["consents", "utility", "personal", "review"]

    async def _nothing():
        return None

    monkeypatch.setattr(template_service, "load_recs_from_db", _nothing)
    monkeypatch.setattr(app_main, "_validate_dataspace_config", _nothing)
    monkeypatch.setattr(app_main, "_validate_admin_config", lambda: None)
    monkeypatch.setattr(app_main, "_validate_provisioning_config", lambda: None)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "sms_provider", "log")
    monkeypatch.setattr(settings, "require_encryption", False)

    with caplog.at_level(logging.WARNING, logger=app_main.logger.name):
        async with app_main.lifespan(FastAPI()):
            pass

    assert "Document upload and scanning are disabled" in caplog.text


def test_the_warning_names_what_is_missing(switch, caplog):
    switch(dpa=False, key="sk-test")
    with caplog.at_level(logging.WARNING, logger=app_main.logger.name):
        app_main._warn_document_processing()

    assert "EXTRACTION_ENABLED not set" in caplog.text
    assert "EXTRACTION_API_KEY" not in caplog.text


def test_the_old_names_are_reported_and_do_not_switch_it_on(switch, monkeypatch, caplog):
    """Renamed on 2026-09-14, with no alias: a leftover leaves the feature off, and says so."""
    switch(dpa=False, key="")
    monkeypatch.setattr(settings, "removed_dpa_signed", "yes")
    monkeypatch.setattr(settings, "removed_openai_api_key", "sk-test")

    with caplog.at_level(logging.WARNING, logger=app_main.logger.name):
        app_main._warn_document_processing()

    assert settings.document_processing_enabled is False
    assert "DPA_SIGNED (now EXTRACTION_ENABLED)" in caplog.text
    assert "OPENAI_API_KEY (now EXTRACTION_API_KEY)" in caplog.text
    assert "Document upload and scanning are disabled" in caplog.text


def test_the_old_names_are_read_from_the_environment_under_their_own_names(monkeypatch):
    from celine.onboarding.config.settings import Settings

    monkeypatch.setenv("DPA_SIGNED", "yes")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("EXTRACTION_ENABLED", raising=False)
    monkeypatch.delenv("EXTRACTION_API_KEY", raising=False)
    fresh = Settings(_env_file=None)

    assert fresh.removed_dpa_signed == "yes"
    assert fresh.removed_openai_api_key == "sk-test"
    assert fresh.extraction_enabled is False
    assert fresh.extraction_api_key == ""


def test_no_warning_when_the_switch_is_on(switch, caplog):
    switch(dpa=True, key="sk-test")
    with caplog.at_level(logging.WARNING, logger=app_main.logger.name):
        app_main._warn_document_processing()

    assert caplog.text == ""


# ── what the wizard is told ───────────────────────────────────────────────────


@pytest.mark.parametrize(("dpa", "key", "enabled"), [(True, "sk-test", True), (False, "", False)])
def test_config_reports_the_switch(switch, client, dpa, key, enabled):
    switch(dpa=dpa, key=key)
    features = client.get("/api/rec-a/config").json()["features"]
    assert features["document_upload"] is enabled
    assert features["document_scan"] is enabled


# ── the API refuses ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("method", "url", "kwargs"), GATED)
def test_off_every_document_route_answers_403(switch, client, method, url, kwargs):
    switch(dpa=False, key="")
    res = getattr(client, method)(url, **kwargs)

    assert res.status_code == 403
    assert res.json()["detail"]["code"] == deps.DOCUMENT_PROCESSING_DISABLED


def test_off_nothing_reaches_the_extractor(switch, client, monkeypatch):
    from celine.onboarding.extractors import openai_extractor

    called = []

    async def _extract(self, pages, **kw):
        called.append(pages)
        return {}, {}

    monkeypatch.setattr(openai_extractor.OpenAIExtractor, "extract_pages", _extract)
    switch(dpa=False, key="")

    client.post("/api/rec-a/extract", files=FILE)
    client.post("/api/rec-a/extract-id", files=FILE)

    assert called == []


@pytest.mark.parametrize("url", ["/api/rec-a/extract", "/api/rec-a/extract-id"])
def test_on_the_stateless_scans_work(switch, client, monkeypatch, url):
    from celine.onboarding.extractors import openai_extractor

    async def _extract(self, pages, **kw):
        return {"nome": "TEST"}, {}

    monkeypatch.setattr(openai_extractor.OpenAIExtractor, "extract_pages", _extract)
    switch(dpa=True, key="sk-test")

    res = client.post(url, files=FILE)

    assert res.status_code == 200
    assert res.json() == {"nome": "TEST"}


@pytest.mark.parametrize(("method", "url", "kwargs"), GATED[2:])
def test_on_the_stateful_routes_get_past_the_switch(
    switch, client, monkeypatch, method, url, kwargs
):
    """Past the gate, each reaches its own lookup and finds nothing: 404, not 403."""
    from celine.onboarding.services import document_service, extraction_service, submission_service

    async def _none(*_a, **_kw):
        return None

    monkeypatch.setattr(document_service, "get_document", _none)
    monkeypatch.setattr(extraction_service, "get_extraction", _none)
    monkeypatch.setattr(submission_service, "get_submission", _none)
    switch(dpa=True, key="sk-test")

    res = getattr(client, method)(url, **kwargs)

    assert res.status_code == 404


def test_off_an_upload_that_is_not_for_scanning_is_not_refused(switch, client, monkeypatch):
    from celine.onboarding.services import submission_service

    async def _none(*_a, **_kw):
        return None

    monkeypatch.setattr(submission_service, "get_submission", _none)
    switch(dpa=False, key="")

    res = client.post(
        f"/api/rec-a/submissions/{uuid.uuid4()}/documents?doc_type=other",
        files={"file": FILE["files"]},
    )

    assert res.status_code == 404
