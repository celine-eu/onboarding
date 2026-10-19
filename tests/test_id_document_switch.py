"""The identity document has its own switch, apart from the bill (REQ-0051).

Two levels, both required: the deployment's kill switch (`ID_DOCUMENT_ENABLED`, on by
default, and under document processing) and the template's `id_document: {enabled: …}`
(absent means enabled). With either off, the wizard is told so in `/config`, and the
routes that store or read an ID card answer 403 `id_document_disabled` while the bill
keeps its upload and scan.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import celine.onboarding.main as app_main
from celine.onboarding.api import deps
from celine.onboarding.config.settings import Settings, settings
from celine.onboarding.models.database import get_db
from celine.onboarding.models.document import DocumentType
from celine.onboarding.services import template_service as ts

ENDPOINT = "http://models.internal/v1"
REC = "example-rec"
SESSION = SimpleNamespace(id=uuid.uuid4(), rec_slug=REC)
FILE = ("doc.pdf", b"%PDF-1.4", "application/pdf")


@pytest.fixture()
def switches(monkeypatch, seed_rec):
    """Set document processing, the deployment's ID switch and the template's block."""

    def _set(*, processing: bool = True, deployment: bool = True, template=None):
        monkeypatch.setattr(settings, "extraction_enabled", processing)
        monkeypatch.setattr(settings, "llm_base_url", ENDPOINT)
        monkeypatch.setattr(settings, "llm_vision_model", "vision-model")
        monkeypatch.setattr(settings, "id_document_enabled", deployment)
        for removed in (
            "removed_dpa_signed",
            "removed_openai_api_key",
            "removed_extraction_api_key",
            "removed_extraction_base_url",
            "removed_extraction_model",
        ):
            monkeypatch.setattr(settings, removed, "")
        manifest = {} if template is None else {"id_document": template}
        seed_rec(REC, **manifest)

    return _set


@pytest.fixture()
def client():
    from celine.onboarding.api.config import router as config_router
    from celine.onboarding.api.documents import router as documents_router
    from celine.onboarding.api.extractions import router as extractions_router

    app = FastAPI()
    app.state.limiter = deps.limiter
    for router in (config_router, documents_router, extractions_router):
        app.include_router(router, prefix="/api/{rec_slug}")

    async def _no_db():
        yield None

    async def _the_session():
        return SESSION

    app.dependency_overrides[get_db] = _no_db
    app.dependency_overrides[deps.require_session] = _the_session
    return TestClient(app)


@pytest.fixture()
def stored(monkeypatch):
    """A stored document of the given type, owned by the session, with an extraction.

    The bill's extraction already exists and is confirmed, so a bill route that gets
    past every switch answers 409 rather than calling the model.
    """
    from celine.onboarding.services import document_service, extraction_service

    def _store(doc_type: DocumentType):
        document = SimpleNamespace(
            id=uuid.uuid4(),
            submission_id=SESSION.id,
            doc_type=doc_type,
            extraction=object() if doc_type != DocumentType.ID_CARD else None,
        )
        extraction = SimpleNamespace(
            document_id=document.id, confirmed_by_user=doc_type != DocumentType.ID_CARD
        )

        async def _doc(_db, _id):
            return document

        async def _ext(_db, _id):
            return extraction

        monkeypatch.setattr(document_service, "get_document", _doc)
        monkeypatch.setattr(extraction_service, "get_extraction", _ext)
        return document

    return _store


# ── the setting ───────────────────────────────────────────────────────────────


def test_the_deployment_switch_defaults_on_and_is_read_from_the_environment(monkeypatch):
    """
    @verifies REQ-0051
    """
    monkeypatch.delenv("ID_DOCUMENT_ENABLED", raising=False)
    assert Settings(_env_file=None).id_document_enabled is True
    monkeypatch.setenv("ID_DOCUMENT_ENABLED", "false")
    assert Settings(_env_file=None).id_document_enabled is False


@pytest.mark.parametrize(
    ("processing", "deployment", "enabled"),
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_the_deployment_state_needs_document_processing_too(
    switches, processing, deployment, enabled
):
    """
    @verifies REQ-0051
    """
    switches(processing=processing, deployment=deployment)
    assert settings.id_document_processing_enabled is enabled


# ── the template key ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("block", [None, {}, {"enabled": True}, {"enabled": False}])
def test_a_valid_block_is_accepted(block):
    """
    @verifies REQ-0051
    """
    ts.validate_id_document(block, where="t")


@pytest.mark.parametrize(
    ("block", "match"),
    [
        (False, "must be a mapping"),
        ({"enabled": "no"}, "true or false"),
        ({"enabled": 0}, "true or false"),
        ({"enabled": False, "scan": False}, "unknown keys"),
    ],
    ids=["not-a-mapping", "enabled-a-string", "enabled-an-int", "unknown-key"],
)
def test_anything_else_is_refused(block, match):
    """
    @verifies REQ-0051
    """
    with pytest.raises(ValueError, match=match):
        ts.validate_id_document(block, where="t")


async def test_boot_refuses_a_malformed_block(seed_rec):
    """Manifests come from the database and may predate this build: boot re-validates.

    @verifies REQ-0051
    """
    seed_rec(REC, id_document={"enabled": "off"})
    with pytest.raises(ValueError, match="id_document.enabled"):
        await app_main._validate_dataspace_config()


@pytest.mark.parametrize(
    ("manifest", "offered"),
    [({}, True), ({"id_document": {}}, True), ({"id_document": {"enabled": False}}, False)],
    ids=["absent", "empty", "off"],
)
def test_an_absent_block_offers_the_id_document(manifest, offered):
    """
    @verifies REQ-0051
    """
    assert ts.id_document_offered(manifest) is offered


# ── what the wizard is told ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("processing", "deployment", "template", "bill", "id_document"),
    [
        (False, True, None, False, False),
        (True, False, {"enabled": True}, True, False),
        (True, True, {"enabled": False}, True, False),
        (True, True, None, True, True),
        (True, True, {"enabled": True}, True, True),
    ],
    ids=[
        "processing-off",
        "kill-switch-off-template-on",
        "template-off",
        "both-on-absent",
        "both-on-explicit",
    ],
)
def test_config_reports_the_id_document_apart_from_the_bill(
    switches, client, processing, deployment, template, bill, id_document
):
    """
    @verifies REQ-0051
    """
    switches(processing=processing, deployment=deployment, template=template)
    features = client.get(f"/api/{REC}/config").json()["features"]
    assert features["document_upload"] is bill
    assert features["document_scan"] is bill
    assert features["id_document_upload"] is id_document
    assert features["id_document_scan"] is id_document


def test_the_template_switch_is_per_community(switches, seed_rec, client):
    """
    @verifies REQ-0051
    """
    switches(template={"enabled": False})
    seed_rec("other-rec")
    assert client.get(f"/api/{REC}/config").json()["features"]["id_document_upload"] is False
    assert client.get("/api/other-rec/config").json()["features"]["id_document_upload"] is True


# ── the API refuses the identity document, and only it ────────────────────────

OFF = [
    {"deployment": False},
    {"template": {"enabled": False}},
]


@pytest.mark.parametrize("off", OFF, ids=["deployment", "template"])
@pytest.mark.parametrize(
    "route",
    ["extract-id", "upload", "stored-extract", "stored-confirm"],
)
def test_off_every_id_document_route_answers_403(switches, client, stored, off, route):
    """
    @verifies REQ-0051
    """
    switches(**off)
    document = stored(DocumentType.ID_CARD)
    if route == "extract-id":
        res = client.post(f"/api/{REC}/extract-id", files={"files": FILE})
    elif route == "upload":
        res = client.post(
            f"/api/{REC}/submissions/{SESSION.id}/documents?doc_type=id_card",
            files={"file": FILE},
        )
    elif route == "stored-extract":
        res = client.post(f"/api/{REC}/documents/{document.id}/extract")
    else:
        res = client.post(
            f"/api/{REC}/extractions/{uuid.uuid4()}/confirm", json={"extracted_data": {}}
        )

    assert res.status_code == 403
    assert res.json()["detail"]["code"] == deps.ID_DOCUMENT_DISABLED


def test_with_processing_off_the_code_stays_document_processing_disabled(switches, client):
    """The broader switch answers first, whatever the template says."""
    switches(processing=False, template={"enabled": False})
    res = client.post(f"/api/{REC}/extract-id", files={"files": FILE})

    assert res.status_code == 403
    assert res.json()["detail"]["code"] == deps.DOCUMENT_PROCESSING_DISABLED


@pytest.mark.parametrize("off", OFF, ids=["deployment", "template"])
def test_off_the_bill_is_still_scanned(switches, client, monkeypatch, off):
    """
    @verifies REQ-0051
    """
    from celine.onboarding.extractors import openai_extractor
    from celine.onboarding.services import extraction_service

    async def _extract(self, pages, **kw):
        return {"pod": "IT001E00000000"}, {}

    async def _record(*_a, **_kw):
        return None

    monkeypatch.setattr(openai_extractor.OpenAIExtractor, "extract_pages", _extract)
    monkeypatch.setattr(extraction_service, "record_scan", _record)
    switches(**off)

    res = client.post(f"/api/{REC}/extract", files={"files": FILE})

    assert res.status_code == 200
    assert res.json() == {"pod": "IT001E00000000"}


@pytest.mark.parametrize("off", OFF, ids=["deployment", "template"])
def test_off_a_bill_upload_gets_past_the_switches(switches, client, monkeypatch, off):
    """Past the gates, the upload reaches its submission lookup and finds nothing: 404.

    @verifies REQ-0051
    """
    from celine.onboarding.services import submission_service

    async def _none(*_a, **_kw):
        return None

    monkeypatch.setattr(submission_service, "get_submission", _none)
    switches(**off)

    res = client.post(
        f"/api/{REC}/submissions/{SESSION.id}/documents?doc_type=utility_bill",
        files={"file": FILE},
    )

    assert res.status_code == 404


@pytest.mark.parametrize("off", OFF, ids=["deployment", "template"])
@pytest.mark.parametrize("route", ["stored-extract", "stored-confirm"])
def test_off_a_stored_bill_gets_past_the_switches(switches, client, stored, off, route):
    """The bill's extraction exists and is confirmed, so getting past the gates is a 409.

    @verifies REQ-0051
    """
    switches(**off)
    document = stored(DocumentType.UTILITY_BILL)
    if route == "stored-extract":
        res = client.post(f"/api/{REC}/documents/{document.id}/extract")
    else:
        res = client.post(
            f"/api/{REC}/extractions/{uuid.uuid4()}/confirm", json={"extracted_data": {}}
        )

    assert res.status_code == 409


def test_on_the_id_document_routes_get_past_the_switch(switches, client, monkeypatch):
    """
    @verifies REQ-0051
    """
    from celine.onboarding.extractors import openai_extractor
    from celine.onboarding.services import extraction_service

    async def _extract(self, pages, **kw):
        return {"nome": "TEST"}, {}

    async def _record(*_a, **_kw):
        return None

    monkeypatch.setattr(openai_extractor.OpenAIExtractor, "extract_pages", _extract)
    monkeypatch.setattr(extraction_service, "record_scan", _record)
    switches()

    res = client.post(f"/api/{REC}/extract-id", files={"files": FILE})

    assert res.status_code == 200
    assert res.json() == {"nome": "TEST"}


# ── startup ───────────────────────────────────────────────────────────────────


def test_boot_says_the_id_document_is_off_everywhere(switches, caplog):
    import logging

    switches(deployment=False)
    with caplog.at_level(logging.INFO, logger=app_main.logger.name):
        app_main._warn_document_processing()

    assert "ID_DOCUMENT_ENABLED=false" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
