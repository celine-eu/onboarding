"""What a submission's emails carry, and to whom.

The participant gets a receipt with no link. Each operator gets a message of their
own, with a link to the submission's page in the admin console — which needs a
sign-in and records each document opened — and never a link to the files.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import SubmissionStatus
from celine.onboarding.services import email_service, notification_service

SUBMISSION_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
OPERATORS = ["ops-a@rec.example.org", "ops-b@rec.example.org"]


@pytest.fixture()
def rec(seed_rec):
    return seed_rec(
        "rec-a",
        name="REC A",
        notifications={
            "from": "noreply@rec.example.org",
            "notify": OPERATORS,
            "base_url": "https://onboarding.rec.example.org",
        },
    )


def _submission(email: str | None = "applicant@example.org"):
    return SimpleNamespace(
        id=SUBMISSION_ID,
        ref="20261005-abcd",
        rec_slug="rec-a",
        email=email,
        status=SubmissionStatus.SUBMITTED,
        created_at=datetime(2026, 10, 5, 9, 0, tzinfo=UTC),
        documents=[],
    )


REVIEW = "https://onboarding.rec.example.org/admin/rec-a/submissions/" + str(SUBMISSION_ID)


class TestMessages:
    def test_one_message_per_address(self, rec):
        """
        @verifies REQ-0031
        """
        messages = email_service.build_messages(_submission(), review_url=REVIEW)
        assert sorted(m["To"] for m in messages) == sorted(["applicant@example.org", *OPERATORS])
        for m in messages:
            assert "," not in m["To"]
            assert m["Cc"] is None and m["Bcc"] is None

    def test_the_participant_receipt_carries_no_link(self, rec):
        """
        @verifies REQ-0031
        """
        messages = email_service.build_messages(_submission(), review_url=REVIEW)
        receipt = next(m for m in messages if m["To"] == "applicant@example.org")
        body = receipt.get_content()
        assert "http" not in body
        assert "20261005-abcd" in body

    def test_operators_get_the_console_not_a_file(self, rec):
        """
        @verifies REQ-0031
        """
        for m in email_service.build_messages(_submission(), review_url=REVIEW):
            if m["To"] in OPERATORS:
                body = m.get_content()
                assert REVIEW in body
                assert "/api/" not in body

    def test_no_participant_address_still_notifies_operators(self, rec):
        messages = email_service.build_messages(_submission(email=None), review_url=REVIEW)
        assert sorted(m["To"] for m in messages) == sorted(OPERATORS)

    def test_each_message_is_sent_on_its_own(self, rec, monkeypatch):
        """
        @verifies REQ-0031
        """
        server = MagicMock()
        smtp = MagicMock()
        smtp.return_value.__enter__.return_value = server
        monkeypatch.setattr(email_service.smtplib, "SMTP", smtp)
        monkeypatch.setattr(settings, "smtp_host", "smtp.rec.example.org")
        monkeypatch.setattr(settings, "smtp_tls", False)
        monkeypatch.setattr(settings, "smtp_user", "")

        email_service.send_submission_email(_submission(), review_url=REVIEW)

        sent = [c.args[0]["To"] for c in server.send_message.call_args_list]
        assert sorted(sent) == sorted(["applicant@example.org", *OPERATORS])


class TestNotification:
    def test_the_review_url_is_the_console_page(self, rec):
        """
        @verifies REQ-0031
        """
        assert notification_service.review_url(_submission()) == REVIEW

    async def test_the_email_gets_the_console_even_with_a_storage_link(self, seed_rec, monkeypatch):
        """A storage backend's link is presigned: it opens the file for whoever holds it.

        @verifies REQ-0031
        """
        from celine.onboarding.outputs.base import StorageResult

        seed_rec(
            "rec-a",
            notifications={
                "notify": OPERATORS,
                "base_url": "https://onboarding.rec.example.org",
                "storage": {"type": "s3"},
            },
        )

        class _Backend:
            async def upload_submission(self, ref, pdf, docs):
                return StorageResult(backend_name="s3", folder_url="https://s3.example.org/x?sig=1")

        monkeypatch.setattr(notification_service, "get_backend", lambda *a: _Backend())
        monkeypatch.setattr(
            "celine.onboarding.services.pdf_service.generate_submission_pdf", lambda s: b"%PDF"
        )
        monkeypatch.setattr(settings, "smtp_host", "smtp.rec.example.org")
        sent = {}

        def _send(submission, review_url=None, **kwargs):
            sent.update(review_url=review_url, **kwargs)

        monkeypatch.setattr(email_service, "send_submission_email", _send)

        await notification_service.handle_submission_notification(_submission())

        assert sent == {"review_url": REVIEW}


def test_there_is_no_public_download_route():
    """
    @verifies REQ-0031
    """
    from celine.onboarding.main import app

    paths = list(app.openapi()["paths"])
    assert "/api/{rec_slug}/submissions/{submission_id}" in paths
    assert not [p for p in paths if p.startswith("/api/downloads")]
