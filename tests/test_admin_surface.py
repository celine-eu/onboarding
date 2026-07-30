"""The rest of the console's API: masking, pagination, documents, exports, stats.

The masking tests are the ones that matter. `fiscal_code` and `pod_code` are
encrypted at rest and were previously handed back in clear to anyone holding the
admin token — encryption that is undone at the last hop protects the backup tape
and nothing else.
"""

from __future__ import annotations

import uuid
from datetime import UTC

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from celine.onboarding.api.admin import create_admin_router
from celine.onboarding.api.admin.masking import mask_value
from celine.onboarding.security.middleware import AdminAuthMiddleware
from celine.onboarding.services import audit_service, document_service, submission_service

ORG = "community-a"
SUBMISSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
FISCAL_CODE = "RSSMRA85T10A562S"
POD_CODE = "IT001E12345678"


def build_submission():
    from datetime import datetime

    from celine.onboarding.models.submission import Submission, SubmissionStatus

    now = datetime.now(UTC)
    submission = Submission(
        id=SUBMISSION_ID,
        ref="20260730-aaa1",
        rec_slug="rec-a",
        status=SubmissionStatus.UNDER_REVIEW,
        consent_ip="10.0.0.1",
        session_token="stub",
        first_name="Mario",
        last_name="Rossi",
        email="mario@example.org",
        fiscal_code=FISCAL_CODE,
        pod_code=POD_CODE,
    )
    for field, value in {
        "gdpr_consent": True,
        "policy_consent": True,
        "statute_consent": True,
        "keep_me_updated": False,
        "phone_verified": False,
        "phone_verified_at": None,
        "phone": None,
        "notes": None,
        "share_provisioned": False,
        "data_sharing_consent": False,
        "dataspace_subject_id": None,
        "dataspace_did": None,
        "dataspace_vc_id": None,
        "dataspace_vc_issued_at": None,
        "created_at": now,
        "updated_at": now,
        "last_active_at": now,
    }.items():
        setattr(submission, field, value)
    return submission


@pytest.fixture()
def audited(monkeypatch):
    entries: list[dict] = []

    async def _record(db, **kwargs):
        entries.append(kwargs)

    monkeypatch.setattr(audit_service, "record_and_commit", _record)
    return entries


@pytest.fixture()
def client(seed_rec, issue_token, audited, monkeypatch) -> TestClient:
    seed_rec("rec-a", name="REC A", organization=ORG)

    from celine.onboarding.api.admin import documents as documents_api
    from celine.onboarding.api.admin import submissions as submissions_api

    async def _owned(db, submission_id, rec_slug):
        from fastapi import HTTPException

        if submission_id != SUBMISSION_ID:
            raise HTTPException(404, "Submission not found")
        return build_submission()

    monkeypatch.setattr(submissions_api, "_owned_submission", _owned)
    monkeypatch.setattr(documents_api, "_owned_submission", _owned)

    async def _list(db, **kwargs):
        return [build_submission()]

    async def _count(db, **kwargs):
        return 137

    monkeypatch.setattr(submission_service, "list_submissions", _list)
    monkeypatch.setattr(submission_service, "count_submissions", _count)

    app = FastAPI()
    app.add_middleware(AdminAuthMiddleware)
    app.include_router(create_admin_router())
    return TestClient(app)


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


BASE = "/api/admin/rec-a"
ONE = f"{BASE}/submissions/{SUBMISSION_ID}"


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


class TestMaskValue:
    def test_keeps_a_recognisable_tail(self):
        assert mask_value("RSSMRA85T10A562S") == "••••••••••••562S"

    def test_preserves_length(self):
        """A truncated mask would make a malformed code look well-formed."""
        assert len(mask_value("IT001E12345678")) == len("IT001E12345678")

    def test_short_values_reveal_nothing(self):
        assert mask_value("AB") == "••"
        assert mask_value("ABCD") == "••••"

    def test_empty_stays_empty(self):
        assert mask_value(None) is None
        assert mask_value("") == ""


class TestMasking:
    def test_the_list_is_always_masked(self, client, operator_token):
        body = client.get(f"{BASE}/submissions", headers=auth(operator_token(ORG, "admins"))).json()
        assert body[0]["fiscal_code"] == mask_value(FISCAL_CODE)
        assert body[0]["pod_code"] == mask_value(POD_CODE)

    def test_the_detail_is_masked_by_default(self, client, operator_token):
        body = client.get(ONE, headers=auth(operator_token(ORG, "admins"))).json()
        assert body["fiscal_code"] == mask_value(FISCAL_CODE)

    def test_names_and_email_are_not_masked(self, client, operator_token):
        """An operator cannot work a queue of anonymous rows."""
        body = client.get(ONE, headers=auth(operator_token(ORG, "viewers"))).json()
        assert body["first_name"] == "Mario"
        assert body["email"] == "mario@example.org"

    def test_a_viewer_cannot_reveal(self, client, operator_token):
        response = client.get(f"{ONE}?reveal=true", headers=auth(operator_token(ORG, "viewers")))
        assert response.status_code == 403
        assert "reveal capability" in response.json()["detail"]

    def test_an_editor_can_reveal(self, client, operator_token):
        body = client.get(f"{ONE}?reveal=true", headers=auth(operator_token(ORG, "editors"))).json()
        assert body["fiscal_code"] == FISCAL_CODE
        assert body["pod_code"] == POD_CODE

    def test_revealing_is_audited_distinctly(self, client, operator_token, audited):
        """ "Somebody opened this record" and "somebody unmasked it" are different acts."""
        client.get(f"{ONE}?reveal=true", headers=auth(operator_token(ORG, "editors")))
        assert audited[-1]["action"] == "reveal"
        assert "unmasked" in audited[-1]["detail"]

    def test_viewing_without_reveal_is_audited_as_a_view(self, client, operator_token, audited):
        client.get(ONE, headers=auth(operator_token(ORG, "editors")))
        assert audited[-1]["action"] == "view"


# ---------------------------------------------------------------------------
# Pagination and filters
# ---------------------------------------------------------------------------


class TestQueue:
    def test_reports_the_total(self, client, operator_token):
        """Without it the console cannot tell a full last page from a full page."""
        response = client.get(f"{BASE}/submissions", headers=auth(operator_token(ORG, "viewers")))
        assert response.headers["X-Total-Count"] == "137"

    def test_filters_reach_the_query(self, client, operator_token, monkeypatch):
        seen: dict = {}

        async def _list(db, **kwargs):
            seen.update(kwargs)
            return []

        monkeypatch.setattr(submission_service, "list_submissions", _list)

        client.get(
            f"{BASE}/submissions?status=submitted&ref=aaa1&skip=10&limit=5",
            headers=auth(operator_token(ORG, "viewers")),
        )
        assert seen["status"].value == "submitted"
        assert seen["ref"] == "aaa1"
        assert seen["skip"] == 10
        assert seen["limit"] == 5

    def test_the_count_uses_the_same_filters(self, client, operator_token, monkeypatch):
        """A total computed over a different filter is worse than no total."""
        seen: dict = {}

        async def _count(db, **kwargs):
            seen.update(kwargs)
            return 0

        monkeypatch.setattr(submission_service, "count_submissions", _count)

        client.get(
            f"{BASE}/submissions?status=approved&ref=xyz",
            headers=auth(operator_token(ORG, "viewers")),
        )
        assert seen["status"].value == "approved"
        assert seen["ref"] == "xyz"
        assert "skip" not in seen and "limit" not in seen

    def test_limit_is_bounded(self, client, operator_token):
        response = client.get(
            f"{BASE}/submissions?limit=5000", headers=auth(operator_token(ORG, "viewers"))
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


class TestDocuments:
    @pytest.fixture()
    def a_document(self, monkeypatch):
        from celine.onboarding.models.document import Document, DocumentType

        document = Document(
            id=uuid.uuid4(),
            submission_id=SUBMISSION_ID,
            doc_type=DocumentType.UTILITY_BILL,
            file_path="rec-a/x.pdf",
            original_filename="bolletta.pdf",
            mime_type="application/pdf",
            size_bytes=1234,
        )
        # server_default only materialises on flush; nothing is persisted here.
        document.created_at = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )

        async def _get(db, document_id):
            return document if document_id == document.id else None

        async def _list(db, submission_id):
            return [document]

        monkeypatch.setattr(document_service, "get_document", _get)
        monkeypatch.setattr(document_service, "list_documents", _list)
        monkeypatch.setattr(document_service, "read_file", lambda doc: b"%PDF-1.4 fake")
        return document

    def test_list_needs_only_read(self, client, operator_token, a_document):
        response = client.get(f"{ONE}/documents", headers=auth(operator_token(ORG, "viewers")))
        assert response.status_code == 200
        assert response.json()[0]["original_filename"] == "bolletta.pdf"

    def test_download_returns_the_decrypted_bytes(self, client, operator_token, a_document):
        response = client.get(
            f"{ONE}/documents/{a_document.id}", headers=auth(operator_token(ORG, "viewers"))
        )
        assert response.status_code == 200
        assert response.content == b"%PDF-1.4 fake"
        assert "bolletta.pdf" in response.headers["content-disposition"]

    def test_download_is_audited(self, client, operator_token, a_document, audited):
        """The bill carries the address, supply point and consumption history."""
        client.get(f"{ONE}/documents/{a_document.id}", headers=auth(operator_token(ORG, "viewers")))
        assert audited[-1]["action"] == "download_document"

    def test_listing_is_not_audited(self, client, operator_token, a_document, audited):
        before = len(audited)
        client.get(f"{ONE}/documents", headers=auth(operator_token(ORG, "viewers")))
        assert len(audited) == before

    def test_a_document_from_elsewhere_is_404(self, client, operator_token, a_document):
        """Pairing a foreign document id with a local submission id must not work."""
        response = client.get(
            f"{ONE}/documents/{uuid.uuid4()}",
            headers=auth(operator_token(ORG, "viewers")),
        )
        assert response.status_code == 404

    def test_a_missing_file_is_410_not_500(self, client, operator_token, a_document, monkeypatch):
        def _gone(doc):
            raise FileNotFoundError

        monkeypatch.setattr(document_service, "read_file", _gone)
        response = client.get(
            f"{ONE}/documents/{a_document.id}", headers=auth(operator_token(ORG, "viewers"))
        )
        assert response.status_code == 410


# ---------------------------------------------------------------------------
# Exports and stats
# ---------------------------------------------------------------------------


class TestExports:
    @pytest.fixture()
    def stub_exports(self, monkeypatch, tmp_path):
        from celine.onboarding.api.admin import exports as exports_api

        monkeypatch.setattr(exports_api, "_staging_dir", lambda: tmp_path)

        async def _csv(db, path, **kwargs):
            from pathlib import Path

            Path(path).write_text("ref\n20260730-aaa1\n")
            return 1

        async def _pods(db, path, **kwargs):
            from pathlib import Path

            Path(path).write_text("pod\nIT001E12345678\n")
            return 1

        monkeypatch.setattr(exports_api, "export_submissions_csv", _csv)
        monkeypatch.setattr(exports_api, "export_pod_list", _pods)

    def test_viewer_cannot_export(self, client, operator_token, stub_exports):
        response = client.post(
            f"{BASE}/exports/csv", json={}, headers=auth(operator_token(ORG, "viewers"))
        )
        assert response.status_code == 403

    def test_manager_can_export(self, client, operator_token, stub_exports):
        response = client.post(
            f"{BASE}/exports/csv", json={}, headers=auth(operator_token(ORG, "managers"))
        )
        assert response.status_code == 200
        assert response.text.startswith("ref\n")

    def test_the_file_does_not_survive_the_request(
        self, client, operator_token, stub_exports, tmp_path
    ):
        """A console that left a copy per download would accumulate PII on disk."""
        client.post(f"{BASE}/exports/csv", json={}, headers=auth(operator_token(ORG, "managers")))
        assert list(tmp_path.iterdir()) == []

    def test_export_is_audited_with_the_recipient(
        self, client, operator_token, stub_exports, audited
    ):
        client.post(
            f"{BASE}/exports/csv",
            json={"recipient_ref": "distributor-x"},
            headers=auth(operator_token(ORG, "managers")),
        )
        assert audited[-1]["action"] == "export_csv"
        assert "distributor-x" in audited[-1]["detail"]

    def test_pod_list_requires_an_offer(self, client, operator_token, stub_exports):
        """Consent is purpose-scoped, so a handover has to name the offer."""
        response = client.post(
            f"{BASE}/exports/pod-list",
            json={"recipient_ref": "distributor-x"},
            headers=auth(operator_token(ORG, "managers")),
        )
        assert response.status_code == 422


class TestStats:
    def test_reports_every_status_including_zero(self, client, operator_token, monkeypatch):
        async def _queue(db, *, rec_slug):
            return {"draft": 0, "submitted": 3, "under_review": 1, "approved": 9, "rejected": 0}

        async def _enable(db, *, rec_slug):
            return {"submissions_with_failed_steps": 2}

        monkeypatch.setattr(submission_service, "queue_stats", _queue)
        monkeypatch.setattr(submission_service, "enablement_stats", _enable)

        body = client.get(f"{BASE}/stats", headers=auth(operator_token(ORG, "viewers"))).json()
        assert body["by_status"]["submitted"] == 3
        assert body["by_status"]["rejected"] == 0
        assert body["submissions_with_failed_steps"] == 2
