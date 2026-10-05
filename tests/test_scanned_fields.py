"""What a scan read is the service's record, not the applicant's.

The wizard shows the applicant what their bill and identity document were read as,
and the operator compares the declared name, tax code and POD against it. So the
applicant's own PATCH cannot change it: the service keeps the scan as it read it,
and the applicant corrects the declared fields. An operator still may, through the
admin API, which records who did.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from celine.onboarding.models.document import DocumentType
from celine.onboarding.models.schemas import ExtractionConfirm
from celine.onboarding.models.submission import SubmissionStatus
from celine.onboarding.services import extraction_service, submission_service

SUBMISSION_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture()
def wizard(seed_rec, monkeypatch):
    """The participant's PATCH route; returns (client, what the service was given)."""
    from celine.onboarding.api import submissions as api
    from celine.onboarding.models.database import get_db

    seed_rec("rec-a")
    sub = SimpleNamespace(id=SUBMISSION_ID, status=SubmissionStatus.DRAFT, rec_slug="rec-a")

    async def _live(submission_id, request, *, rec_slug=None, db=None):
        return sub

    given = []

    async def _update(db, submission, data, background_tasks=None):
        given.append(data)
        raise ValueError("stop here")

    async def _db():
        yield None

    monkeypatch.setattr(api, "_get_live_submission", _live)
    monkeypatch.setattr(submission_service, "update_submission", _update)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/{rec_slug}")
    app.dependency_overrides[get_db] = _db
    return TestClient(app), given


class TestTheParticipantPatch:
    def test_scanned_values_are_dropped(self, wizard):
        """
        @verifies REQ-0034
        """
        client, given = wizard
        client.patch(
            f"/api/rec-a/submissions/{SUBMISSION_ID}",
            json={
                "first_name": "Mario",
                "fiscal_code": "RSSMRA85T10A562S",
                "extracted_data": {"pod": "IT001E99999999", "codice_fiscale": "XXX"},
                "id_extracted_data": {"codice_fiscale": "XXX", "scadenza": "01/01/2099"},
            },
        )
        (data,) = given
        assert not data.model_fields_set & {"extracted_data", "id_extracted_data"}
        assert data.first_name == "Mario"
        assert data.fiscal_code == "RSSMRA85T10A562S"


class TestTheScanIsKept:
    async def test_a_scan_is_recorded_on_the_draft(self):
        """
        @verifies REQ-0034
        """
        sub = SimpleNamespace(status=SubmissionStatus.DRAFT, extracted_data=None)
        db = SimpleNamespace(commit=AsyncMock())
        await extraction_service.record_scan(
            db, sub, id_card=False, data={"pod": "IT001E12345678", "consumo": "9"}
        )
        assert sub.extracted_data["pod"] == "IT001E12345678"
        assert "consumo" not in sub.extracted_data
        db.commit.assert_awaited()

    async def test_a_later_poorer_read_does_not_erase_a_good_one(self):
        sub = SimpleNamespace(
            status=SubmissionStatus.DRAFT, id_extracted_data={"nome": "MARIO", "cognome": "ROSSI"}
        )
        db = SimpleNamespace(commit=AsyncMock())
        await extraction_service.record_scan(
            db, sub, id_card=True, data={"nome": "", "cognome": "ROSSI BIANCHI"}
        )
        assert sub.id_extracted_data["nome"] == "MARIO"
        assert sub.id_extracted_data["cognome"] == "ROSSI BIANCHI"

    async def test_a_submitted_application_is_not_rescanned(self):
        sub = SimpleNamespace(status=SubmissionStatus.SUBMITTED, extracted_data={"pod": "A"})
        db = SimpleNamespace(commit=AsyncMock())
        await extraction_service.record_scan(db, sub, id_card=False, data={"pod": "BBBB"})
        assert sub.extracted_data == {"pod": "A"}
        db.commit.assert_not_awaited()


class TestConfirmingAnExtraction:
    def _extraction(self):
        return SimpleNamespace(
            extracted_data={"pod": "IT001E12345678", "nome": "MARIO"},
            document=SimpleNamespace(doc_type=DocumentType.UTILITY_BILL),
            confirmed_by_user=False,
            confirmed_at=None,
        )

    async def test_an_edit_is_refused(self):
        """
        @verifies REQ-0034
        """
        extraction = self._extraction()
        db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
        with pytest.raises(extraction_service.ScannedValueEditError, match="pod"):
            await extraction_service.confirm_extraction(
                db, extraction, ExtractionConfirm(extracted_data={"pod": "IT001E99999999"})
            )
        assert extraction.extracted_data["pod"] == "IT001E12345678"
        assert extraction.confirmed_by_user is False

    async def test_confirming_what_was_read_is_accepted(self):
        extraction = self._extraction()
        db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
        await extraction_service.confirm_extraction(
            db, extraction, ExtractionConfirm(extracted_data={"pod": "IT001E12345678"})
        )
        assert extraction.confirmed_by_user is True
