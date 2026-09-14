"""Approval waits for the REC's recorded verification, not for a document.

The community verifies a participant's identity and that they hold the POD on its
own responsibility — offline, or by confirming a document stored on the
submission — and records it. Approval refuses until it has. A correction is a new
verification that supersedes the old one; nothing is edited away.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from celine.onboarding.api.admin import create_admin_router
from celine.onboarding.models.submission import Submission, SubmissionStatus
from celine.onboarding.models.verification import VerificationMethod
from celine.onboarding.security.middleware import AdminAuthMiddleware
from celine.onboarding.services import audit_service, document_service, review, verification
from celine.onboarding.services.audit_service import Actor

ORG = "community-a"
SUBMISSION_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
OPERATOR = Actor(type="user", sub="op-1", email="operator@example.org")


def make_submission(status: SubmissionStatus = SubmissionStatus.UNDER_REVIEW) -> Submission:
    """A real, unpersisted `Submission`: the admin read validates its shape."""
    now = datetime.now(UTC)
    submission = Submission(
        id=SUBMISSION_ID,
        ref="20260914-ver1",
        rec_slug="rec-a",
        status=status,
        consent_ip="10.0.0.1",
        session_token="stub",
    )
    for field, value in {
        "gdpr_consent": True,
        "policy_consent": True,
        "statute_consent": True,
        "keep_me_updated": False,
        "phone_verified": False,
        "share_provisioned": False,
        "data_sharing_consent": False,
        "created_at": now,
        "updated_at": now,
    }.items():
        setattr(submission, field, value)
    return submission


class FakeDb:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def refresh(self, _row):
        pass


@pytest.fixture()
def trail(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(audit_service, "record", lambda db, **kw: rows.append(kw))
    return rows


@pytest.fixture()
def documents(monkeypatch):
    """Documents by id; each is `(id, submission_id)`."""
    stored: dict[uuid.UUID, object] = {}

    async def _get(db, document_id):
        return stored.get(document_id)

    monkeypatch.setattr(document_service, "get_document", _get)

    def _add(submission_id: uuid.UUID) -> uuid.UUID:
        from types import SimpleNamespace

        doc_id = uuid.uuid4()
        stored[doc_id] = SimpleNamespace(id=doc_id, submission_id=submission_id)
        return doc_id

    return _add


# ── the gate ──────────────────────────────────────────────────────────────────


def test_approval_is_refused_without_a_verification(seed_rec):
    seed_rec("rec-a", steps=["consents", "personal", "review"])
    with pytest.raises(ValueError, match="no verification"):
        review.check(make_submission(), SubmissionStatus.APPROVED)


async def test_approval_is_allowed_once_one_is_recorded(seed_rec, trail):
    seed_rec("rec-a", steps=["consents", "personal", "review"])
    submission = make_submission()
    await verification.record(
        FakeDb(), submission, method=VerificationMethod.OFFLINE, actor=OPERATOR
    )

    review.check(submission, SubmissionStatus.APPROVED)


def test_only_approval_waits_for_it(seed_rec):
    seed_rec("rec-a", steps=["consents", "personal", "review"])
    review.check(make_submission(), SubmissionStatus.REJECTED)


def test_an_object_without_verifications_is_refused_not_waved_through(seed_rec):
    from types import SimpleNamespace

    seed_rec("rec-a", steps=["consents", "personal", "review"])
    bare = SimpleNamespace(
        rec_slug="rec-a", status=SubmissionStatus.UNDER_REVIEW, phone_verified=False
    )
    with pytest.raises(ValueError, match="no verification"):
        review.check(bare, SubmissionStatus.APPROVED)


# ── recording ─────────────────────────────────────────────────────────────────


async def test_offline_is_recorded_with_who_and_audited(trail):
    db = FakeDb()
    submission = make_submission()

    row = await verification.record(
        db,
        submission,
        method=VerificationMethod.OFFLINE,
        actor=OPERATOR,
        note="  ID seen at the office  ",
        ip="10.0.0.9",
    )

    assert submission.verification is row
    assert (row.method, row.actor_email, row.note) == (
        "offline",
        "operator@example.org",
        "ID seen at the office",
    )
    assert db.commits == 1
    assert [r["action"] for r in trail] == ["verification_recorded"]
    assert trail[0]["detail"] == "method=offline"
    # The note is free text about a person and stays out of the trail.
    assert "office" not in trail[0]["detail"]


async def test_a_new_verification_supersedes_and_both_stay(trail, documents):
    submission = make_submission()
    first = await verification.record(
        FakeDb(), submission, method=VerificationMethod.OFFLINE, actor=OPERATOR
    )
    doc = documents(SUBMISSION_ID)
    second = await verification.record(
        FakeDb(),
        submission,
        method=VerificationMethod.UPLOADED_DOCUMENT,
        document_id=doc,
        actor=OPERATOR,
    )

    assert submission.verifications == [first, second]
    assert submission.verification is second
    assert trail[1]["action"] == "verification_superseded"
    assert trail[1]["detail"] == f"method=uploaded-document document={doc} supersedes={first.id}"


async def test_uploaded_document_must_name_one(trail):
    with pytest.raises(verification.VerificationError, match="must name the document"):
        await verification.record(
            FakeDb(),
            make_submission(),
            method=VerificationMethod.UPLOADED_DOCUMENT,
            actor=OPERATOR,
        )


@pytest.mark.parametrize("whose", ["another submission", "nobody"])
async def test_uploaded_document_must_be_on_this_submission(trail, documents, whose):
    doc = documents(uuid.uuid4()) if whose == "another submission" else uuid.uuid4()
    submission = make_submission()

    with pytest.raises(verification.VerificationError, match="not stored on this submission"):
        await verification.record(
            FakeDb(),
            submission,
            method=VerificationMethod.UPLOADED_DOCUMENT,
            document_id=doc,
            actor=OPERATOR,
        )
    assert submission.verifications == []
    assert trail == []


async def test_offline_names_no_document(trail, documents):
    with pytest.raises(verification.VerificationError, match="names no document"):
        await verification.record(
            FakeDb(),
            make_submission(),
            method=VerificationMethod.OFFLINE,
            document_id=documents(SUBMISSION_ID),
            actor=OPERATOR,
        )


@pytest.mark.parametrize(
    "status", [SubmissionStatus.DRAFT, SubmissionStatus.APPROVED, SubmissionStatus.REJECTED]
)
async def test_only_a_pending_submission_takes_one(trail, status):
    with pytest.raises(verification.VerificationError, match="only while"):
        await verification.record(
            FakeDb(), make_submission(status), method=VerificationMethod.OFFLINE, actor=OPERATOR
        )


def test_the_credential_value_is_prefixed():
    assert VerificationMethod.OFFLINE.credential_value == "submission-review:offline"
    assert (
        VerificationMethod.UPLOADED_DOCUMENT.credential_value
        == "submission-review:uploaded-document"
    )


# ── the admin API ─────────────────────────────────────────────────────────────


@pytest.fixture()
def api(monkeypatch, seed_rec, issue_token, trail):
    seed_rec("rec-a", name="REC A", organization=ORG, steps=["consents", "personal", "review"])
    from celine.onboarding.api.admin import submissions as submissions_api
    from celine.onboarding.api.admin import verifications as verifications_api
    from celine.onboarding.models.database import get_db

    state = {"submission": make_submission()}

    async def _owned(db, submission_id, rec_slug):
        from fastapi import HTTPException

        if submission_id != SUBMISSION_ID:
            raise HTTPException(404, "Submission not found")
        return state["submission"]

    monkeypatch.setattr(submissions_api, "_owned_submission", _owned)
    monkeypatch.setattr(verifications_api, "_owned_submission", _owned)

    app = FastAPI()
    app.add_middleware(AdminAuthMiddleware)
    app.include_router(create_admin_router())

    async def _db():
        yield FakeDb()

    app.dependency_overrides[get_db] = _db
    return TestClient(app), state


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


URL = f"/api/admin/rec-a/submissions/{SUBMISSION_ID}/verifications"


@pytest.mark.parametrize(("group", "status"), [("managers", 201), ("editors", 403)])
def test_recording_needs_the_review_capability(api, operator_token, group, status):
    client, _ = api
    res = client.post(URL, json={"method": "offline"}, headers=auth(operator_token(ORG, group)))
    assert res.status_code == status


def test_the_recorded_verification_is_on_the_submission_and_listed(api, operator_token):
    client, _ = api
    headers = auth(operator_token(ORG, "managers"))
    created = client.post(URL, json={"method": "offline", "note": "checked"}, headers=headers)
    assert created.status_code == 201
    assert created.json()["actor_type"] == "user"

    viewer = auth(operator_token(ORG, "viewers"))
    listed = client.get(URL, headers=viewer).json()
    shown = client.get(f"/api/admin/rec-a/submissions/{SUBMISSION_ID}", headers=viewer).json()

    assert [v["method"] for v in listed] == ["offline"]
    assert shown["verification"]["id"] == created.json()["id"]


def test_an_approved_submission_answers_409(api, operator_token):
    client, state = api
    state["submission"] = make_submission(SubmissionStatus.APPROVED)
    res = client.post(
        URL, json={"method": "offline"}, headers=auth(operator_token(ORG, "managers"))
    )
    assert res.status_code == 409


def test_a_bad_document_answers_422(api, operator_token, documents):
    client, _ = api
    res = client.post(
        URL,
        json={"method": "uploaded-document", "document_id": str(uuid.uuid4())},
        headers=auth(operator_token(ORG, "managers")),
    )
    assert res.status_code == 422


def test_an_unknown_method_answers_422(api, operator_token):
    client, _ = api
    res = client.post(
        URL, json={"method": "trust-me"}, headers=auth(operator_token(ORG, "managers"))
    )
    assert res.status_code == 422
