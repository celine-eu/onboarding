"""An operator corrects a member's declared POD, names and email, as a revision.

From `submitted` on these four fields change only through a revision: the operator
says how they checked the new value and why, the row keeps the value it replaced,
and the column changes in the same transaction. Nothing is propagated yet; the
outcome says what a later step will have to carry.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from celine.onboarding.api.admin import create_admin_router
from celine.onboarding.models.enablement import EnablementStatus
from celine.onboarding.models.submission import Submission, SubmissionStatus
from celine.onboarding.security.middleware import AdminAuthMiddleware
from celine.onboarding.services import audit_service, document_service, enablement, revision
from celine.onboarding.services import rec_registry as rr
from celine.onboarding.services import template_service as ts
from celine.onboarding.services.audit_service import Actor
from celine.onboarding.services.revision import (
    PropagationStep,
    RevisableField,
    RevisionError,
    RevisionMethod,
    RevisionStatusError,
)

ORG = "community-a"
SUBMISSION_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
OPERATOR = Actor(type="user", sub="op-1", email="operator@example.org")
MEMBER = Actor(type="user", sub="member-1", email="member@example.org")

DECLARED_POD = "IT001E00000001"
CORRECT_POD = "IT001E00000002"
THIRD_POD = "IT001E00000003"


def make_submission(status: SubmissionStatus = SubmissionStatus.UNDER_REVIEW) -> Submission:
    now = datetime.now(UTC)
    submission = Submission(
        id=SUBMISSION_ID,
        ref="20261001-rev1",
        rec_slug="rec-a",
        status=status,
        consent_ip="10.0.0.1",
        session_token="wizard-session",
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.org",
        pod_code=DECLARED_POD,
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
        "last_active_at": now,
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
def steps(monkeypatch):
    """The enablement rows `load_steps` returns, by step name."""
    rows: dict[str, object] = {}

    async def _load(db, submission_id):
        return rows

    monkeypatch.setattr(enablement, "load_steps", _load)
    return rows


@pytest.fixture()
def documents(monkeypatch):
    stored: dict[uuid.UUID, object] = {}

    async def _get(db, document_id):
        return stored.get(document_id)

    monkeypatch.setattr(document_service, "get_document", _get)

    def _add(submission_id: uuid.UUID) -> uuid.UUID:
        doc_id = uuid.uuid4()
        stored[doc_id] = SimpleNamespace(id=doc_id, submission_id=submission_id)
        return doc_id

    return _add


async def revise(
    submission,
    field=RevisableField.POD_CODE,
    value=CORRECT_POD,
    *,
    db=None,
    method=RevisionMethod.OFFLINE,
    note="Checked against the bill on the phone",
    **kw,
):
    return await revision.record(
        db or FakeDb(),
        submission,
        field=field,
        value=value,
        method=method,
        note=note,
        actor=kw.pop("actor", OPERATOR),
        **kw,
    )


# ── the record ────────────────────────────────────────────────────────────────


async def test_the_column_changes_with_the_row_in_one_commit(trail, steps):
    db = FakeDb()
    submission = make_submission()

    outcome = await revise(submission, db=db)

    row = outcome.revision
    assert submission.pod_code == CORRECT_POD
    assert submission.revisions == [row]
    assert (row.field, row.previous_value, row.new_value) == ("pod_code", DECLARED_POD, CORRECT_POD)
    assert (row.method, row.actor_type, row.actor_sub, row.actor_email) == (
        "offline",
        "operator",
        "op-1",
        "operator@example.org",
    )
    assert row.note == "Checked against the bill on the phone"
    # The revision, the column and the audit row commit together.
    assert db.commits == 1
    assert [r["action"] for r in trail] == ["revise"]


async def test_append_only_and_the_newest_is_in_force(trail, steps):
    submission = make_submission()
    first = (await revise(submission)).revision
    second = (await revise(submission, value=THIRD_POD)).revision

    assert submission.revisions == [first, second]
    assert revision.history(submission, RevisableField.POD_CODE) == [first, second]
    assert submission.pod_code == THIRD_POD
    assert second.previous_value == CORRECT_POD
    # The first one is untouched by the second.
    assert (first.previous_value, first.new_value) == (DECLARED_POD, CORRECT_POD)
    assert trail[1]["detail"].endswith(f"supersedes={first.id}")


async def test_the_first_revision_keeps_what_the_person_declared(trail, steps):
    submission = make_submission()
    assert revision.declared_value(submission, RevisableField.POD_CODE) == DECLARED_POD

    await revise(submission)
    await revise(submission, value=THIRD_POD)

    assert revision.declared_value(submission, RevisableField.POD_CODE) == DECLARED_POD


async def test_fields_have_separate_histories(trail, steps):
    submission = make_submission()
    await revise(submission)
    await revise(submission, RevisableField.EMAIL, "ada.l@example.org")

    assert len(revision.history(submission, RevisableField.POD_CODE)) == 1
    assert len(revision.history(submission, RevisableField.EMAIL)) == 1
    assert trail[1]["detail"].count("supersedes") == 0


@pytest.mark.parametrize("note", [None, "", "   "])
async def test_an_operator_must_write_a_note(trail, steps, note):
    submission = make_submission()
    with pytest.raises(RevisionError, match="needs a note"):
        await revise(submission, note=note)
    assert submission.revisions == []
    assert submission.pod_code == DECLARED_POD
    assert trail == []


async def test_the_pod_is_checked_and_upper_cased(trail, steps):
    submission = make_submission()
    await revise(submission, value=f"  {CORRECT_POD.lower()} ")
    assert submission.pod_code == CORRECT_POD

    with pytest.raises(RevisionError, match="Invalid POD"):
        await revise(submission, value="IT-not-a-pod")


@pytest.mark.parametrize("value", ["", "   "])
async def test_a_revision_does_not_erase_a_value(trail, steps, value):
    with pytest.raises(RevisionError, match="needs a value"):
        await revise(make_submission(), RevisableField.LAST_NAME, value)


async def test_the_value_already_held_is_refused(trail, steps):
    with pytest.raises(RevisionError, match="already holds"):
        await revise(make_submission(), value=DECLARED_POD)


# ── statuses (R14) ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [SubmissionStatus.DRAFT, SubmissionStatus.REJECTED])
async def test_a_draft_or_rejected_submission_takes_none(trail, steps, status):
    submission = make_submission(status)
    with pytest.raises(RevisionStatusError, match="submitted, under review or approved"):
        await revise(submission)
    assert submission.pod_code == DECLARED_POD


@pytest.mark.parametrize("status", [SubmissionStatus.SUBMITTED, SubmissionStatus.UNDER_REVIEW])
async def test_before_approval_nothing_is_to_propagate(trail, steps, status):
    outcome = await revise(make_submission(status))
    assert outcome.propagation == ()
    assert outcome.skip_reason is None


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (
            RevisableField.POD_CODE,
            CORRECT_POD,
            (PropagationStep.REGISTRY_DELIVERY_POINT, PropagationStep.CONSENT_KEYS),
        ),
        (
            RevisableField.FIRST_NAME,
            "Augusta",
            (PropagationStep.ACCOUNT_PROFILE, PropagationStep.REGISTRY_NAME),
        ),
        (
            RevisableField.EMAIL,
            "ada.l@example.org",
            (
                PropagationStep.ACCOUNT_PROFILE,
                PropagationStep.INVITATION,
                PropagationStep.IDENTITY_MAPPING,
            ),
        ),
    ],
)
async def test_after_approval_the_outcome_names_the_steps(trail, steps, field, value, expected):
    steps["keycloak_user"] = SimpleNamespace(
        status=EnablementStatus.SUCCEEDED, completed_at=datetime.now(UTC)
    )
    outcome = await revise(make_submission(SubmissionStatus.APPROVED), field, value)

    assert outcome.propagation == expected
    assert outcome.skip_reason is None
    assert (outcome.field, outcome.new_value) == (field, value)


async def test_on_a_revoked_member_it_is_recorded_and_every_step_skipped(trail, steps):
    steps["keycloak_user"] = SimpleNamespace(
        status=EnablementStatus.PENDING, completed_at=datetime.now(UTC)
    )
    submission = make_submission(SubmissionStatus.APPROVED)

    outcome = await revise(submission)

    assert submission.pod_code == CORRECT_POD
    assert outcome.propagation  # the steps exist, each to be recorded skipped
    assert outcome.skip_reason == "enablement revoked"


def test_a_step_that_never_ran_is_not_revoked():
    assert not enablement.revoked(
        {"keycloak_user": SimpleNamespace(status=EnablementStatus.PENDING, completed_at=None)}
    )


# ── evidence ──────────────────────────────────────────────────────────────────


async def test_uploaded_document_names_one_on_this_submission(trail, steps, documents):
    submission = make_submission()
    doc = documents(SUBMISSION_ID)

    outcome = await revise(submission, method=RevisionMethod.UPLOADED_DOCUMENT, document_id=doc)

    assert outcome.revision.document_id == doc
    assert f"document={doc}" in trail[0]["detail"]


@pytest.mark.parametrize("whose", ["another submission", "nobody", "none named"])
async def test_uploaded_document_must_be_this_submissions(trail, steps, documents, whose):
    doc = {
        "another submission": lambda: documents(uuid.uuid4()),
        "nobody": uuid.uuid4,
        "none named": lambda: None,
    }[whose]()
    submission = make_submission()

    with pytest.raises(RevisionError, match="not stored on this submission|must name"):
        await revise(submission, method=RevisionMethod.UPLOADED_DOCUMENT, document_id=doc)
    assert submission.revisions == []
    assert submission.pod_code == DECLARED_POD


async def test_offline_names_no_document(trail, steps, documents):
    with pytest.raises(RevisionError, match="names no document"):
        await revise(make_submission(), document_id=documents(SUBMISSION_ID))


async def test_an_operator_cannot_claim_a_member_session(trail, steps):
    with pytest.raises(RevisionError, match="cannot use method"):
        await revise(make_submission(), method=RevisionMethod.MEMBER_SESSION)


# ── the member, later (R13): the service already takes them ───────────────────


async def test_the_service_accepts_a_member_correcting_their_own_name(trail, steps):
    submission = make_submission(SubmissionStatus.APPROVED)

    outcome = await revise(
        submission,
        RevisableField.LAST_NAME,
        "King",
        method=RevisionMethod.MEMBER_SESSION,
        note=None,
        actor=MEMBER,
        policy=revision.MEMBER,
    )

    assert submission.last_name == "King"
    assert (outcome.revision.actor_type, outcome.revision.method) == ("member", "member-session")
    assert outcome.revision.note is None
    assert outcome.propagation == (PropagationStep.ACCOUNT_PROFILE, PropagationStep.REGISTRY_NAME)


async def test_the_pod_stays_the_operators(trail, steps):
    with pytest.raises(RevisionError, match="member cannot revise pod_code"):
        await revise(
            make_submission(SubmissionStatus.APPROVED),
            method=RevisionMethod.MEMBER_SESSION,
            actor=MEMBER,
            policy=revision.MEMBER,
        )


async def test_a_member_cannot_claim_operator_evidence(trail, steps):
    with pytest.raises(RevisionError, match="cannot use method offline"):
        await revise(
            make_submission(SubmissionStatus.APPROVED),
            RevisableField.EMAIL,
            "ada.l@example.org",
            actor=MEMBER,
            policy=revision.MEMBER,
        )


# ── no values in the trail or the log ─────────────────────────────────────────


async def test_the_trail_and_the_log_hold_no_values(trail, steps, caplog):
    caplog.set_level(logging.DEBUG)
    submission = make_submission()
    note = "Called the member, read the bill"

    await revise(submission, note=note)
    await revise(submission, RevisableField.EMAIL, "ada.l@example.org", note=note)

    written = " ".join(r["detail"] for r in trail) + " " + caplog.text
    for value in (DECLARED_POD, CORRECT_POD, "ada@example.org", "ada.l@example.org", note):
        assert value not in written
    assert trail[0]["detail"].startswith("field=pod_code revision=")


# ── approval reads the corrected columns ──────────────────────────────────────


async def test_the_registry_member_is_built_from_the_corrected_pod(trail, steps):
    submission = make_submission()
    await revise(submission)
    await revise(submission, RevisableField.FIRST_NAME, "Augusta")

    payload = rr.build_member_payload(
        submission,
        ts.RecRegistryBinding(community="test-community", default_area="north", areas={}),
    )

    assert [dp["id"] for dp in payload["delivery_points"]] == [CORRECT_POD]
    assert payload["name"] == "Augusta Lovelace"


# ── the admin API ─────────────────────────────────────────────────────────────


@pytest.fixture()
def api(monkeypatch, seed_rec, trail, steps):
    seed_rec("rec-a", name="REC A", organization=ORG, steps=["consents", "personal", "review"])
    from celine.onboarding.api.admin import revisions as revisions_api
    from celine.onboarding.api.admin import submissions as submissions_api
    from celine.onboarding.models.database import get_db

    state = {"submission": make_submission()}

    async def _owned(db, submission_id, rec_slug):
        from fastapi import HTTPException

        if submission_id != SUBMISSION_ID:
            raise HTTPException(404, "Submission not found")
        return state["submission"]

    monkeypatch.setattr(submissions_api, "_owned_submission", _owned)
    monkeypatch.setattr(revisions_api, "_owned_submission", _owned)

    async def _record_and_commit(db, **kw):
        trail.append(kw)

    monkeypatch.setattr(audit_service, "record_and_commit", _record_and_commit)

    app = FastAPI()
    app.add_middleware(AdminAuthMiddleware)
    app.include_router(create_admin_router())

    async def _db():
        yield FakeDb()

    app.dependency_overrides[get_db] = _db
    return TestClient(app), state


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


URL = f"/api/admin/rec-a/submissions/{SUBMISSION_ID}/revisions"
BODY = {"field": "pod_code", "value": CORRECT_POD, "method": "offline", "note": "bill"}


@pytest.mark.parametrize(
    ("group", "status"), [("admins", 201), ("managers", 201), ("editors", 403), ("viewers", 403)]
)
def test_recording_needs_the_revise_capability(api, operator_token, group, status):
    client, state = api
    res = client.post(URL, json=BODY, headers=auth(operator_token(ORG, group)))
    assert res.status_code == status, res.text
    if status == 403:
        assert state["submission"].pod_code == DECLARED_POD


def test_the_pod_is_masked_unless_revealed(api, operator_token):
    client, _ = api
    created = client.post(URL, json=BODY, headers=auth(operator_token(ORG, "managers")))
    assert created.json()["new_value"] == "••••••••••0002"
    assert created.json()["actor_type"] == "operator"

    viewer = auth(operator_token(ORG, "viewers"))
    listed = client.get(URL, headers=viewer).json()
    assert [r["previous_value"] for r in listed] == ["••••••••••0001"]
    assert client.get(f"{URL}?reveal=true", headers=viewer).status_code == 403

    editor = auth(operator_token(ORG, "editors"))
    revealed = client.get(f"{URL}?reveal=true", headers=editor).json()
    assert [(r["previous_value"], r["new_value"]) for r in revealed] == [
        (DECLARED_POD, CORRECT_POD)
    ]


def test_names_are_not_masked(api, operator_token):
    client, _ = api
    body = {**BODY, "field": "first_name", "value": "Augusta"}
    res = client.post(URL, json=body, headers=auth(operator_token(ORG, "managers")))
    assert (res.json()["previous_value"], res.json()["new_value"]) == ("Ada", "Augusta")


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "note": ""},
        {k: v for k, v in BODY.items() if k != "note"},
        {**BODY, "field": "fiscal_code", "value": "not-a-fiscal-code"},
        {**BODY, "method": "member-session"},
        {**BODY, "value": "not-a-pod"},
        {**BODY, "method": "uploaded-document", "document_id": str(uuid.uuid4())},
    ],
    ids=["blank note", "no note", "bad fiscal code", "member method", "bad pod", "foreign document"],
)
def test_bad_requests_answer_422(api, operator_token, documents, body):
    client, state = api
    res = client.post(URL, json=body, headers=auth(operator_token(ORG, "managers")))
    assert res.status_code == 422, res.text
    assert state["submission"].revisions == []


@pytest.mark.parametrize("status", [SubmissionStatus.DRAFT, SubmissionStatus.REJECTED])
def test_a_draft_or_rejected_submission_answers_409(api, operator_token, status):
    client, state = api
    state["submission"] = make_submission(status)
    res = client.post(URL, json=BODY, headers=auth(operator_token(ORG, "managers")))
    assert res.status_code == 409


def test_an_approved_submission_takes_one(api, operator_token):
    client, state = api
    state["submission"] = make_submission(SubmissionStatus.APPROVED)
    res = client.post(URL, json=BODY, headers=auth(operator_token(ORG, "managers")))
    assert res.status_code == 201
    assert state["submission"].pod_code == CORRECT_POD


# ── the admin PATCH guard ─────────────────────────────────────────────────────

PATCH_URL = f"/api/admin/rec-a/submissions/{SUBMISSION_ID}"


@pytest.mark.parametrize("field", ["pod_code", "first_name", "last_name", "email"])
@pytest.mark.parametrize(
    "status",
    [
        SubmissionStatus.SUBMITTED,
        SubmissionStatus.UNDER_REVIEW,
        SubmissionStatus.APPROVED,
        SubmissionStatus.REJECTED,
    ],
)
def test_the_admin_patch_refuses_revised_fields_from_submitted_on(
    api, operator_token, field, status
):
    client, state = api
    state["submission"] = make_submission(status)
    value = CORRECT_POD if field == "pod_code" else "changed@example.org"
    res = client.patch(PATCH_URL, json={field: value}, headers=auth(operator_token(ORG, "admins")))
    assert res.status_code == 409, res.text
    assert "revision" in res.json()["detail"]
    assert getattr(state["submission"], field) != value


def test_the_admin_patch_still_takes_notes_after_submit(api, operator_token, monkeypatch):
    from celine.onboarding.services import submission_service

    client, state = api
    state["submission"] = make_submission(SubmissionStatus.UNDER_REVIEW)
    update = AsyncMock(return_value=state["submission"])
    monkeypatch.setattr(submission_service, "update_submission", update)

    res = client.patch(
        PATCH_URL, json={"notes": "called"}, headers=auth(operator_token(ORG, "editors"))
    )

    assert res.status_code == 200, res.text
    update.assert_awaited_once()


def test_the_admin_patch_still_edits_a_draft(api, operator_token, monkeypatch):
    from celine.onboarding.services import submission_service

    client, state = api
    state["submission"] = make_submission(SubmissionStatus.DRAFT)
    update = AsyncMock(return_value=state["submission"])
    monkeypatch.setattr(submission_service, "update_submission", update)

    res = client.patch(
        PATCH_URL, json={"pod_code": CORRECT_POD}, headers=auth(operator_token(ORG, "editors"))
    )

    assert res.status_code == 200, res.text


# ── the wizard PATCH guard ────────────────────────────────────────────────────


@pytest.fixture()
def wizard(monkeypatch, bind_rec):
    from celine.onboarding.api import submissions as submissions_api
    from celine.onboarding.models.database import get_db
    from celine.onboarding.services import submission_service

    bind_rec("rec-a")
    state = {"submission": make_submission(SubmissionStatus.DRAFT)}

    async def _get(db, submission_id):
        return state["submission"] if submission_id == SUBMISSION_ID else None

    monkeypatch.setattr(submission_service, "get_submission", _get)

    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    async def _db():
        yield db

    app = FastAPI()
    app.include_router(submissions_api.router, prefix="/api/{rec_slug}")
    app.dependency_overrides[get_db] = _db
    return TestClient(app), state


def _wizard_patch(client, body):
    return client.patch(
        f"/api/rec-a/submissions/{SUBMISSION_ID}",
        json=body,
        headers={"x-session-token": "wizard-session"},
    )


@pytest.mark.parametrize(
    "status",
    [
        SubmissionStatus.SUBMITTED,
        SubmissionStatus.UNDER_REVIEW,
        SubmissionStatus.APPROVED,
        SubmissionStatus.REJECTED,
    ],
)
@pytest.mark.parametrize(
    "body", [{"pod_code": CORRECT_POD}, {"keep_me_updated": True}, {"locale": "en"}]
)
def test_the_wizard_patch_refuses_everything_after_submit(wizard, status, body):
    client, state = wizard
    state["submission"] = make_submission(status)

    res = _wizard_patch(client, body)

    assert res.status_code == 409, res.text
    assert state["submission"].pod_code == DECLARED_POD
    assert state["submission"].keep_me_updated is False
    assert state["submission"].locale is None


def test_the_wizard_patch_still_edits_a_draft(wizard):
    client, state = wizard
    res = _wizard_patch(client, {"pod_code": CORRECT_POD})
    assert res.status_code == 200, res.text
    assert state["submission"].pod_code == CORRECT_POD


# ── the migration ─────────────────────────────────────────────────────────────


class TestTheMigration:
    def test_upgrade_creates_the_table_the_model_describes(self, monkeypatch):
        from test_sharing_intent import _offline_sql

        from celine.onboarding.models.revision import SubmissionRevision

        sql = _offline_sql(monkeypatch, "0016:0017")

        assert "CREATE TABLE submission_revisions" in sql
        for column in SubmissionRevision.__table__.columns:
            assert f"\n    {column.name} " in sql, column.name
        assert "FOREIGN KEY(submission_id) REFERENCES submissions (id) ON DELETE CASCADE" in sql
        assert "FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE SET NULL" in sql
        # The values and the note are ciphertext.
        assert "previous_value TEXT" in sql
        assert "new_value TEXT NOT NULL" in sql
        assert "note TEXT" in sql

    def test_downgrade_drops_it(self, monkeypatch):
        from test_sharing_intent import _offline_sql

        sql = _offline_sql(monkeypatch, "0017:0016", downgrade=True)
        assert "DROP TABLE submission_revisions" in sql
