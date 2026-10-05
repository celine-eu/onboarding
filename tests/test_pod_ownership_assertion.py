# ruff: noqa: F811 — fixtures are imported by name and requested as parameters
"""The community's assertion that a member holds the supply points it shares (R4).

Evidence digests at upload and on the verification (REQ-0041), no plain offline
check where grants carry `pod:` keys (REQ-0042), the assertion sent with a grant at a
holder and the holder's refusals explained (REQ-0043), a renewal after approval
(REQ-0044), retention past an erasure (REQ-0045), and a grant refused when the
registry cannot be read (REQ-0046).

Generic names only: example-rec collects, example-dso holds.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import UploadFile
from test_dataspace_shares import (  # noqa: F401 — fixtures, used by name
    HOLDER_URL,
    OWN_OFFER,
    RELEASE,
    _Connectors,
    _enable_shares,
    _org_client,
    _patch_httpx,
    _reset_token_provider,
)
from test_pod_propagation import DECLARED, holder  # noqa: F401 — fixture, used by name
from test_verification import (  # noqa: F401 — fixtures, used by name
    OPERATOR,
    SUBMISSION_ID,
    URL,
    FakeDb,
    api,
    auth,
    documents,
    make_submission,
    trail,
)

import celine.onboarding.services.dataspace_identity as di
from celine.onboarding.config.settings import settings
from celine.onboarding.models.document import DocumentType
from celine.onboarding.models.submission import SubmissionStatus
from celine.onboarding.models.verification import SubmissionVerification, VerificationMethod
from celine.onboarding.services import (
    document_service,
    key_assertion,
    rec_registry,
    service_auth,
    verification,
)

DIGEST = "cd" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _row(**overrides) -> SubmissionVerification:
    fields = {
        "id": uuid.UUID("33333333-3333-3333-3333-333333333333"),
        "method": VerificationMethod.OFFLINE_WITH_EVIDENCE.value,
        "evidence": [{"kind": "utility_bill", "sha256": DIGEST}],
        "actor_type": "user",
        "actor_sub": "operator-sub-1",
        "actor_email": "operator@example.org",
        "actor_client_id": None,
        "created_at": datetime(2026, 10, 5, 9, 30, tzinfo=UTC),
        "note": "checked at the office",
        "last_asserted_at": None,
        "retain_until": None,
    }
    fields.update(overrides)
    return SubmissionVerification(**fields)


@pytest.fixture()
def requires_evidence(monkeypatch):
    """A community that grants members' supply points at a holder, assertion on."""
    monkeypatch.setattr(settings, "ds_key_assertion", True)
    monkeypatch.setattr(key_assertion, "grants_at_a_holder", lambda slug: True)


# ── REQ-0041: digests at upload, copied onto the verification ────────────────


async def test_an_upload_records_the_sha256_of_the_plaintext(monkeypatch, tmp_path):
    """
    @verifies REQ-0041
    """
    from celine.onboarding.services import submission_service

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))

    async def _get(db, submission_id):
        return SimpleNamespace(ref="20261005-r4", rec_slug="rec-a")

    monkeypatch.setattr(submission_service, "get_submission", _get)

    class _Db:
        def add(self, row):
            self.row = row

        async def commit(self):
            pass

        async def refresh(self, row):
            pass

    db = _Db()
    upload = UploadFile(io.BytesIO(PNG), filename="bill.png")
    document = await document_service.save_document(
        db, uuid.uuid4(), upload, DocumentType.UTILITY_BILL
    )

    assert document.sha256 == hashlib.sha256(PNG).hexdigest()
    # What is on disk is the encrypted copy; the digest is of what was uploaded.
    stored = (tmp_path / document.file_path).read_bytes()
    assert hashlib.sha256(stored).hexdigest() != document.sha256 or stored == PNG


async def test_an_uploaded_document_verification_copies_the_digest(trail, documents):
    """
    @verifies REQ-0041
    """
    submission = make_submission()
    doc = documents(SUBMISSION_ID)

    row = await verification.record(
        FakeDb(),
        submission,
        method=VerificationMethod.UPLOADED_DOCUMENT,
        document_id=doc,
        actor=OPERATOR,
    )

    assert row.evidence == [{"kind": "utility_bill", "sha256": "ab" * 32}]
    assert (row.rec_slug, row.submission_ref) == ("rec-a", "20260914-ver1")
    # The trail gets the count, never the digest.
    assert "ab" * 32 not in trail[0]["detail"]
    assert "evidence=1" in trail[0]["detail"]


async def test_an_older_document_is_hashed_from_its_file_when_verified(monkeypatch):
    """
    @verifies REQ-0041
    """
    document = SimpleNamespace(sha256=None, file_path="x")
    monkeypatch.setattr(document_service, "read_file", lambda d: PNG)

    assert document_service.digest(document) == hashlib.sha256(PNG).hexdigest()
    assert document.sha256 == hashlib.sha256(PNG).hexdigest()


def test_the_digest_survives_a_purged_document():
    """The row keeps `evidence` whatever happens to `document_id` (SET NULL).

    @verifies REQ-0041
    """
    row = _row(method="uploaded-document", document_id=None)
    assert key_assertion.build(row)["evidence"] == [{"kind": "utility_bill", "sha256": DIGEST}]


# ── REQ-0042: no plain offline where grants carry pod: keys ──────────────────


async def test_plain_offline_is_refused_where_grants_carry_pod_keys(trail, requires_evidence):
    """
    @verifies REQ-0042
    """
    submission = make_submission()
    with pytest.raises(verification.VerificationError, match="evidence file"):
        await verification.record(
            FakeDb(), submission, method=VerificationMethod.OFFLINE, actor=OPERATOR
        )
    assert submission.verifications == []


async def test_plain_offline_stays_where_no_grant_carries_pod_keys(trail, monkeypatch):
    """Off in dev (the compatibility default), and for a community with no holder.

    @verifies REQ-0042
    """
    monkeypatch.setattr(settings, "ds_key_assertion", None)
    row = await verification.record(
        FakeDb(), make_submission(), method=VerificationMethod.OFFLINE, actor=OPERATOR
    )
    assert row.method == "offline"


async def test_offline_with_evidence_needs_a_digest(trail, requires_evidence):
    """
    @verifies REQ-0042
    """
    with pytest.raises(verification.VerificationError, match="needs the evidence file"):
        await verification.record(
            FakeDb(),
            make_submission(),
            method=VerificationMethod.OFFLINE_WITH_EVIDENCE,
            actor=OPERATOR,
        )
    with pytest.raises(verification.VerificationError, match="sha256"):
        await verification.record(
            FakeDb(),
            make_submission(),
            method=VerificationMethod.OFFLINE_WITH_EVIDENCE,
            evidence=[{"kind": "utility_bill", "sha256": "not-a-digest"}],
            actor=OPERATOR,
        )


def test_the_evidence_route_hashes_the_file_and_stores_nothing(
    api, operator_token, requires_evidence, tmp_path, monkeypatch
):
    """
    @verifies REQ-0042
    """
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    client, state = api
    res = client.post(
        f"{URL}/evidence",
        files=[("files", ("bill.pdf", b"%PDF-1.4 evidence", "application/pdf"))],
        data={"kinds": ["utility_bill"], "note": "seen at the office"},
        headers=auth(operator_token("community-a", "managers")),
    )

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["method"] == "offline-with-evidence"
    assert body["evidence"] == [
        {"kind": "utility_bill", "sha256": hashlib.sha256(b"%PDF-1.4 evidence").hexdigest()}
    ]
    assert list(tmp_path.iterdir()) == []


def test_the_console_is_told_evidence_is_needed(api, operator_token, requires_evidence):
    """
    @verifies REQ-0042
    """
    client, _ = api
    shown = client.get(
        f"/api/admin/rec-a/submissions/{SUBMISSION_ID}",
        headers=auth(operator_token("community-a", "viewers")),
    ).json()
    assert shown["verification_needs_evidence"] is True


def test_plain_offline_answers_422_where_evidence_is_needed(api, operator_token, requires_evidence):
    """
    @verifies REQ-0042
    """
    client, _ = api
    res = client.post(
        URL, json={"method": "offline"}, headers=auth(operator_token("community-a", "managers"))
    )
    assert res.status_code == 422
    assert "hashed, not stored" in res.json()["detail"]


# ── REQ-0043: the assertion ───────────────────────────────────────────────────


def test_the_assertion_carries_codes_and_hashes_only(monkeypatch):
    """
    @verifies REQ-0043
    """
    monkeypatch.setattr(settings, "rec_assertion_hmac_key", "k" * 32)
    row = _row()

    claim = key_assertion.build(row)

    assert set(claim) == {
        "terms",
        "terms_sha256",
        "method",
        "verification_ref",
        "verified_by",
        "verified_at",
        "evidence",
    }
    assert claim["terms"] == "rec-pod-assertion/1"
    assert (
        claim["terms_sha256"]
        == hashlib.sha256(key_assertion.DRAFT_TERMS_FILE.read_bytes()).hexdigest()
    )
    assert claim["method"] == "offline-with-evidence"
    assert claim["verification_ref"] == str(row.id)
    assert (
        claim["verified_by"] == hmac.new(b"k" * 32, b"operator-sub-1", hashlib.sha256).hexdigest()
    )
    assert claim["verified_at"] == "2026-10-05T09:30:00+00:00"
    text = json.dumps(claim)
    for personal in ("operator-sub-1", "operator@example.org", "checked at the office"):
        assert personal not in text


def test_the_draft_terms_say_they_are_a_draft():
    """
    @verifies REQ-0043
    """
    text = key_assertion.DRAFT_TERMS_FILE.read_text()
    assert "DRAFT FOR LEGAL REVIEW" in text
    assert key_assertion.terms().is_draft


@pytest.mark.parametrize(
    "row",
    [None, _row(method="offline", evidence=None), _row(method="uploaded-document", evidence=[])],
)
def test_no_assertion_without_an_evidence_digest(row):
    """
    @verifies REQ-0043
    """
    with pytest.raises(key_assertion.AssertionUnavailableError):
        key_assertion.build(row)


def test_the_switch_is_off_in_dev_and_on_elsewhere(monkeypatch):
    """
    @verifies REQ-0043
    """
    monkeypatch.setattr(settings, "ds_key_assertion", None)
    assert key_assertion.enabled(env="dev") is False
    assert key_assertion.enabled(env="prod") is True
    assert key_assertion.enabled(env="") is True
    monkeypatch.setattr(settings, "ds_key_assertion", True)
    assert key_assertion.enabled(env="dev") is True


@pytest.fixture()
def asserting(holder, submission, monkeypatch):  # noqa: F811
    """The holder world, with the assertion on and a verification with evidence."""
    monkeypatch.setattr(settings, "ds_key_assertion", True)
    submission.verification = _row()
    accepted: list[uuid.UUID] = []

    async def _mark(ref):
        accepted.append(ref)

    monkeypatch.setattr(key_assertion, "mark_accepted", _mark)
    holder.accepted = accepted
    return holder


async def test_a_grant_at_the_holder_carries_the_assertion(submission, asserting):
    """
    @verifies REQ-0043
    """
    assert await di.provision_user_shares(submission, raise_on_error=True) is True

    at_holder = [b for base, b in asserting.posts() if base == HOLDER_URL]
    assert len(at_holder) == 1
    claim = at_holder[0]["legal_basis"]["key_assertion"]
    assert claim["verification_ref"] == str(submission.verification.id)
    assert claim["evidence"] == [{"kind": "utility_bill", "sha256": DIGEST}]
    assert at_holder[0]["keys"] == [f"pod:{DECLARED}"]
    # Nowhere else: the community's own connector gets no keys and no assertion.
    for base, body in asserting.posts():
        if base != HOLDER_URL:
            assert "key_assertion" not in (body.get("legal_basis") or {})
    assert asserting.accepted == [submission.verification.id]


async def test_without_the_switch_nothing_new_is_sent(submission, asserting, monkeypatch):
    """demo3's dev against a connector older than the field.

    @verifies REQ-0043
    """
    monkeypatch.setattr(settings, "ds_key_assertion", False)
    assert await di.provision_user_shares(submission, raise_on_error=True) is True
    for _, body in asserting.posts():
        assert "key_assertion" not in (body.get("legal_basis") or {})


async def test_a_verification_without_evidence_refuses_the_grant_here(submission, asserting):
    """
    @verifies REQ-0043
    """
    submission.verification = _row(method="offline", evidence=None)

    with pytest.raises(ValueError, match="no evidence digest"):
        await di.provision_user_shares(submission, raise_on_error=True)

    assert [base for base, _ in asserting.posts() if base == HOLDER_URL] == []
    assert asserting.accepted == []


def _answer_holder_posts(monkeypatch, fake, status, detail):
    def handler(req):
        url = str(req.url)
        if req.method == "POST" and url.startswith(HOLDER_URL):
            fake.requests.append(req)
            return httpx.Response(status, json={"detail": detail})
        return fake.handler(req)

    _patch_httpx(monkeypatch, handler)


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        (409, "key already held by another subject at this holder", "registered for another"),
        (409, "key suspended by the holder since 2026-10-01", "record a new verification"),
        (422, "key_assertion is required for pod: keys", "did not accept the community"),
        (
            422,
            [{"type": "extra_forbidden", "loc": ["body", "legal_basis", "key_assertion"]}],
            "does not accept the community's assertion yet",
        ),
    ],
)
async def test_the_holders_refusals_are_explained(
    submission, asserting, monkeypatch, status, detail, expected
):
    """
    @verifies REQ-0043
    """
    _answer_holder_posts(monkeypatch, asserting, status, detail)

    with pytest.raises(ValueError) as refused:
        await di.provision_user_shares(submission, raise_on_error=True)

    assert expected in str(refused.value)
    assert asserting.accepted == []


async def test_a_suspended_key_is_shown_in_the_step_report(submission, asserting):
    """
    @verifies REQ-0043
    """
    assert await di.provision_user_shares(submission, raise_on_error=True) is True
    asserting.rows[(HOLDER_URL, RELEASE)]["suspended_keys"] = [f"pod:{DECLARED}"]

    report: list[str] = []
    await di.provision_user_shares(submission, raise_on_error=True, report=report)

    assert any("suspended 1 of the member's supply point" in line for line in report)
    assert all(DECLARED not in line for line in report)


async def test_the_members_page_never_gets_suspended_keys(submission, asserting, monkeypatch):
    """
    @verifies REQ-0043
    """
    from celine.onboarding.services import template_service

    assert await di.provision_user_shares(submission, raise_on_error=True) is True
    asserting.rows[(HOLDER_URL, RELEASE)]["suspended_keys"] = [f"pod:{DECLARED}"]
    rows = await di.subject_shares_at_holders("default", subject_id=submission.dataspace_did)
    assert rows and all("suspended_keys" not in r and "keys" not in r for r in rows)
    assert template_service  # imported for the binding the fixture seeded


async def test_a_relayed_grant_carries_the_assertion(submission, asserting, monkeypatch):
    """The member's own page: the assertion of their newest application here.

    @verifies REQ-0043
    """

    async def _for_subject(rec_slug, subject_id):
        return key_assertion.build(submission.verification), submission.verification.id

    monkeypatch.setattr(key_assertion, "for_subject", _for_subject)

    registrations = await di.relay_member_decision(
        "default",
        subject_id=submission.dataspace_did,
        offer_id=RELEASE,
        enabled=True,
        legal_basis={
            "source": "member",
            "consent_text_version": "1",
            "rendered_text_sha256": "x",
        },
    )

    assert [r.ok for r in registrations] == [True]
    (body,) = [b for base, b in asserting.posts() if base == HOLDER_URL]
    assert body["legal_basis"]["key_assertion"]["method"] == "offline-with-evidence"
    assert asserting.accepted == [submission.verification.id]


# ── REQ-0044: a renewal after approval ───────────────────────────────────────


async def test_an_approved_member_can_be_verified_anew_with_evidence(trail):
    """
    @verifies REQ-0044
    """
    submission = make_submission(SubmissionStatus.APPROVED)

    row = await verification.record(
        FakeDb(),
        submission,
        method=VerificationMethod.OFFLINE_WITH_EVIDENCE,
        evidence=[{"kind": "id_document", "sha256": DIGEST}],
        actor=OPERATOR,
    )

    assert submission.verification is row
    assert trail[-1]["action"] == "verification_renewed"


# ── REQ-0045: retention past an erasure ──────────────────────────────────────


def test_an_erasure_keeps_a_verification_that_backs_a_grant(monkeypatch):
    """
    @verifies REQ-0045
    """
    monkeypatch.setattr(settings, "assertion_retention_years", 10)
    submission = make_submission()
    backing = _row(last_asserted_at=datetime(2026, 10, 5, tzinfo=UTC))
    plain = _row(id=uuid.uuid4())
    submission.verifications.extend([plain, backing])
    now = datetime(2027, 1, 1, tzinfo=UTC)

    kept = verification.retain_on_erasure(submission, now=now)

    assert kept == 1
    assert submission.verifications == [plain]
    assert backing.submission_id is None
    assert backing.retain_until == datetime(2037, 1, 1, tzinfo=UTC)
    assert backing.note is None
    # What the audit needs stays.
    assert backing.evidence == [{"kind": "utility_bill", "sha256": DIGEST}]


async def test_only_expired_rows_are_purged():
    """
    @verifies REQ-0045
    """

    class _Db:
        async def execute(self, statement):
            self.statement = statement
            return SimpleNamespace(rowcount=0)

    db = _Db()
    await verification.purge_expired(db, now=datetime(2030, 1, 1, tzinfo=UTC))
    sql = str(db.statement.compile(compile_kwargs={"literal_binds": True}))
    assert "submission_id IS NULL" in sql
    assert "retain_until IS NOT NULL" in sql
    assert "retain_until <" in sql


def test_the_erasure_route_keeps_backing_rows(api, operator_token, monkeypatch, tmp_path):
    """
    @verifies REQ-0045
    """
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    client, state = api
    submission = state["submission"]
    submission.documents = []
    backing = _row(last_asserted_at=datetime.now(UTC) - timedelta(days=1))
    submission.verifications.append(backing)

    deleted: list = []

    class _Db(FakeDb):
        async def delete(self, row):
            deleted.append(row)

        async def execute(self, statement):
            return SimpleNamespace(rowcount=0)

    from celine.onboarding.models.database import get_db

    async def _db():
        yield _Db()

    client.app.dependency_overrides[get_db] = _db
    res = client.delete(
        f"/api/admin/rec-a/submissions/{SUBMISSION_ID}",
        headers=auth(operator_token("community-a", "admins")),
    )

    assert res.status_code == 204, res.text
    assert deleted == [submission]
    assert backing.submission_id is None and backing.retain_until is not None
    assert submission.verifications == []


# ── REQ-0046: fail closed when the registry cannot be read ───────────────────


async def test_an_unreadable_registry_refuses_the_grant(submission, holder, monkeypatch):  # noqa: F811
    """
    @verifies REQ-0046
    """

    async def _down(dids, *, rec_slug):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(rec_registry, "supply_points_by_did", _down)

    with pytest.raises(ValueError, match="registry could not be read"):
        await di.provision_user_shares(submission, raise_on_error=True)

    assert [base for base, _ in holder.posts() if base == HOLDER_URL] == []


async def test_an_unreadable_registry_refuses_a_relayed_grant(submission, holder, monkeypatch):  # noqa: F811
    """
    @verifies REQ-0046
    """

    async def _down(dids, *, rec_slug):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(rec_registry, "supply_points_by_did", _down)

    registrations = await di.relay_member_decision(
        "default",
        subject_id=submission.dataspace_did,
        offer_id=RELEASE,
        enabled=True,
        legal_basis={"source": "member", "consent_text_version": "1", "rendered_text_sha256": "x"},
    )

    assert [r.ok for r in registrations] == [False]
    assert "registry could not be read" in registrations[0].detail
    assert holder.posts() == []


# ── posture ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def holder_community(monkeypatch):
    monkeypatch.setattr(
        service_auth, "collecting_organisations", lambda: {"example-rec": ["rec-a"]}
    )
    monkeypatch.setattr(key_assertion, "grants_at_a_holder", lambda slug: True)
    monkeypatch.setenv("SVC_DS_COLLECTOR_EXAMPLE_REC_SECRET", "a-real-secret")
    monkeypatch.setattr(settings, "ds_key_assertion", None)


def _names(guard) -> set[str]:
    return {v.setting for v in guard.violations}


def test_outside_dev_the_hmac_key_is_required(holder_community, monkeypatch):
    """
    @verifies REQ-0043
    """
    monkeypatch.setattr(settings, "rec_assertion_hmac_key", "")
    assert "REC_ASSERTION_HMAC_KEY" in _names(service_auth.collector_posture_guard(env="prod"))

    monkeypatch.setattr(settings, "rec_assertion_hmac_key", "its-own-secret")
    assert "REC_ASSERTION_HMAC_KEY" not in _names(service_auth.collector_posture_guard(env="prod"))


def test_outside_dev_the_switch_cannot_be_turned_off(holder_community, monkeypatch):
    """
    @verifies REQ-0043
    """
    monkeypatch.setattr(settings, "ds_key_assertion", False)
    assert "DS_KEY_ASSERTION" in _names(service_auth.collector_posture_guard(env="prod"))


def test_a_community_without_a_holder_needs_neither(monkeypatch):
    monkeypatch.setattr(
        service_auth, "collecting_organisations", lambda: {"example-rec": ["rec-a"]}
    )
    monkeypatch.setattr(key_assertion, "grants_at_a_holder", lambda slug: False)
    monkeypatch.setenv("SVC_DS_COLLECTOR_EXAMPLE_REC_SECRET", "a-real-secret")
    monkeypatch.setattr(settings, "rec_assertion_hmac_key", "")
    names = _names(service_auth.collector_posture_guard(env="prod"))
    assert not names & {"REC_ASSERTION_HMAC_KEY", "DS_KEY_ASSERTION"}


_ = (OWN_OFFER, _Connectors)


def test_digests_hashed_by_the_operator_are_taken_as_json(api, operator_token, requires_evidence):
    """`onboarding-cli review verify --evidence FILE` hashes locally and sends the digest.

    @verifies REQ-0042
    """
    client, _ = api
    res = client.post(
        URL,
        json={
            "method": "offline-with-evidence",
            "evidence": [{"kind": "id_document", "sha256": DIGEST}],
        },
        headers=auth(operator_token("community-a", "managers")),
    )
    assert res.status_code == 201, res.text
    assert res.json()["evidence"] == [{"kind": "id_document", "sha256": DIGEST}]


def test_the_cli_sends_only_the_digest(tmp_path, monkeypatch):
    """
    @verifies REQ-0042
    """
    from typer.testing import CliRunner

    from celine.onboarding.cli import admin as cli_admin

    evidence = tmp_path / "bill.pdf"
    evidence.write_bytes(b"%PDF evidence kept by the community")
    sent: dict = {}

    class _Transport:
        async def verify(self, rec, submission_id, method, document_id, note, *, evidence=None):
            sent.update(method=method, evidence=evidence)
            return {"method": method}

        async def aclose(self):
            pass

    async def _resolve(transport, rec, ref):
        return {"id": "s-1", "ref": ref}

    monkeypatch.setattr(cli_admin, "build", lambda local, **kw: _Transport())
    monkeypatch.setattr(cli_admin, "_resolve", _resolve)

    result = CliRunner().invoke(
        cli_admin.app,
        [
            "review",
            "verify",
            "20261005-r4",
            "--rec",
            "rec-a",
            "--method",
            "offline-with-evidence",
            "--evidence",
            str(evidence),
            "--evidence-kind",
            "utility_bill",
        ],
    )

    assert result.exit_code == 0, result.output
    assert sent == {
        "method": "offline-with-evidence",
        "evidence": [
            {
                "kind": "utility_bill",
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            }
        ],
    }
