"""The submission remembers the language the person used, and nothing else.

Approval hands `submission.locale` to the provisioning service, which writes the
invitation email in it and refuses (`422`) any value outside `it|en|es`. A value
refused *there* fails step 1 closed and blocks approval over a language tag, so
the refusal belongs here, while the wizard is still talking to us.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from celine.onboarding.models.schemas import SubmissionUpdate

REC = "example"
SUBMISSION_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
SESSION = "wizard-session-token"


class TestTheSchema:
    @pytest.mark.parametrize("value", ["it", "en", "es"])
    def test_the_three_ui_languages_are_accepted(self, value):
        assert SubmissionUpdate(locale=value).locale == value

    @pytest.mark.parametrize("value", ["fr", "it-IT", "EN", ""])
    def test_anything_else_is_refused(self, value):
        """`fr` is a real language the service would still refuse; `it-IT` is the
        shape a browser would send; the check is exact, not a normalisation."""
        with pytest.raises(ValidationError):
            SubmissionUpdate(locale=value)

    def test_it_is_optional_and_not_written_when_absent(self):
        """`update_submission` writes only what was sent, so an update without a
        locale must not wipe the one already stored."""
        assert "locale" not in SubmissionUpdate(first_name="Ada").model_dump(exclude_unset=True)


def _submission():
    from celine.onboarding.models.submission import Submission, SubmissionStatus

    now = datetime.now(UTC)
    submission = Submission(
        id=SUBMISSION_ID,
        ref="20260914-abcd",
        rec_slug=REC,
        status=SubmissionStatus.DRAFT,
        consent_ip="10.0.0.1",
        session_token=SESSION,
    )
    for field, value in {
        "gdpr_consent": True,
        "policy_consent": True,
        "statute_consent": False,
        "keep_me_updated": False,
        "phone_verified": False,
        "share_provisioned": False,
        "data_sharing_consent": False,
        "created_at": now,
        "updated_at": now,
        "last_active_at": now,
    }.items():
        setattr(submission, field, value)
    return submission


@pytest.fixture()
def wizard(monkeypatch, bind_rec):
    """The public PATCH route, with the data layer replaced by one real instance."""
    from celine.onboarding.api import submissions as submissions_api
    from celine.onboarding.models.database import get_db
    from celine.onboarding.services import submission_service

    bind_rec(REC)
    stored = _submission()

    async def _get(db, submission_id):
        return stored if submission_id == SUBMISSION_ID else None

    monkeypatch.setattr(submission_service, "get_submission", _get)

    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    async def _db():
        yield db

    app = FastAPI()
    app.include_router(submissions_api.router, prefix="/api/{rec_slug}")
    app.dependency_overrides[get_db] = _db
    return TestClient(app), stored, db


def _patch(client, body):
    return client.patch(
        f"/api/{REC}/submissions/{SUBMISSION_ID}",
        json=body,
        headers={"x-session-token": SESSION},
    )


class TestTheRoute:
    def test_the_locale_is_stored(self, wizard):
        client, stored, db = wizard
        response = _patch(client, {"first_name": "Ada", "locale": "es"})
        assert response.status_code == 200, response.text
        assert stored.locale == "es"
        db.commit.assert_awaited()

    def test_the_last_language_used_wins(self, wizard):
        client, stored, _ = wizard
        assert _patch(client, {"locale": "en"}).status_code == 200
        assert _patch(client, {"locale": "it"}).status_code == 200
        assert stored.locale == "it"

    def test_an_update_without_one_keeps_it(self, wizard):
        client, stored, _ = wizard
        stored.locale = "es"
        assert _patch(client, {"first_name": "Ada"}).status_code == 200
        assert stored.locale == "es"

    def test_an_unsupported_one_is_a_422_and_is_not_stored(self, wizard):
        client, stored, _ = wizard
        response = _patch(client, {"locale": "fr"})
        assert response.status_code == 422
        assert stored.locale is None
