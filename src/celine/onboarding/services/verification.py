"""Recording how the REC verified a participant, before approval.

The community verifies on its own responsibility — offline, or by confirming a
document stored on the submission — and records it here. Approval refuses
without one (`services/review.py`). Recording is its own audited action, so the
trail says who vouched for the person and when, separately from who approved.

**Evidence digests (R4, REQ-0041/0042).** Where the community grants members'
supply points at another participant's connector, its verification is also the
basis of the assertion it sends there (`services/key_assertion.py`), so it must
carry at least one digest of the evidence the community keeps: the uploaded
document's, or the file the operator attaches to an offline check. That file is
hashed and **not stored**: the community keeps its evidence itself, and this
service does not want every member's identity document (see `models/verification`).

**Retention (REQ-0045).** A row a holder accepted an assertion from is not erased
with its submission: it is detached and kept for the retention period.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission, SubmissionStatus
from celine.onboarding.models.verification import SubmissionVerification, VerificationMethod
from celine.onboarding.services import audit_service, document_service, key_assertion
from celine.onboarding.services.audit_service import Actor

logger = logging.getLogger(__name__)

#: A verification is evidence for a pending decision. Once the submission is
#: rejected, correcting it would change what the decision rested on after the
#: fact, so it is refused there.
RECORDABLE = frozenset(
    {SubmissionStatus.SUBMITTED, SubmissionStatus.UNDER_REVIEW, SubmissionStatus.APPROVED}
)

#: Before the decision any method; after approval, only a **renewal** with evidence:
#: the approval stands on the verification it had, and a new one exists to back
#: the community's assertion to a holder — after a holder suspended a supply
#: point, or for a member verified before digests were kept (REQ-0044).
_BEFORE_DECISION = frozenset({SubmissionStatus.SUBMITTED, SubmissionStatus.UNDER_REVIEW})

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: How many evidence files one verification may cite.
MAX_EVIDENCE = 5


class VerificationError(ValueError):
    """The verification cannot be recorded as asked."""


async def record(
    db: AsyncSession,
    submission: Submission,
    *,
    method: VerificationMethod,
    actor: Actor,
    document_id: uuid.UUID | None = None,
    evidence: list[dict[str, str]] | None = None,
    note: str | None = None,
    ip: str | None = None,
    rec_slug: str | None = None,
) -> SubmissionVerification:
    """Record a verification; it supersedes any earlier one.

    ``evidence`` is for ``offline-with-evidence`` only: ``[{"kind", "sha256"}]`` of
    the files the operator attached, hashed by the caller and not stored.
    """
    if submission.status not in RECORDABLE:
        raise VerificationError(
            f"A verification can be recorded only while a submission is submitted, "
            f"under review or approved, not {submission.status.value}"
        )
    renewal = submission.status not in _BEFORE_DECISION
    if renewal and not method.carries_evidence:
        raise VerificationError(
            "After approval a verification can only be renewed with evidence: attach the "
            "evidence file, or choose a document uploaded with the application"
        )

    slug = rec_slug or submission.rec_slug
    digests: list[dict[str, str]] = []
    if method is VerificationMethod.UPLOADED_DOCUMENT:
        if document_id is None:
            raise VerificationError("An uploaded-document verification must name the document")
        if evidence:
            raise VerificationError("An uploaded-document verification attaches no other file")
        document = await document_service.get_document(db, document_id)
        # Same answer for "no such document" and "another submission's document":
        # which documents exist elsewhere is not this caller's business.
        if not document or document.submission_id != submission.id:
            raise VerificationError("The document is not stored on this submission")
        digest = document_service.digest(document)
        if digest:
            digests.append(
                {"kind": key_assertion.evidence_kind(document.doc_type), "sha256": digest}
            )
        elif key_assertion.evidence_required(slug) or renewal:
            raise VerificationError(
                "The document's file is no longer stored, so its digest cannot be kept as "
                "evidence. Attach the evidence file the community keeps instead"
            )
    elif document_id is not None:
        raise VerificationError(f"An {method.value} verification names no document")
    elif method is VerificationMethod.OFFLINE_WITH_EVIDENCE:
        digests = _checked_evidence(evidence)
    else:  # plain offline
        if evidence:
            raise VerificationError(
                "An offline verification with evidence is recorded as offline-with-evidence"
            )
        if key_assertion.evidence_required(slug):
            raise VerificationError(
                "This community shares members' supply points with the grid operator, so an "
                "offline check must come with the evidence file it rested on (it is hashed, "
                "not stored). Attach it, or choose a document uploaded with the application"
            )

    previous = submission.verification
    row = SubmissionVerification(
        # Set here rather than left to the column defaults, which apply only at
        # flush: the caller reads both straight back, and `created_at` is what
        # orders the history.
        id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        method=method.value,
        document_id=document_id,
        evidence=digests or None,
        rec_slug=submission.rec_slug,
        submission_ref=submission.ref,
        note=(note or "").strip() or None,
        actor_type=actor.type,
        actor_sub=actor.sub,
        actor_email=actor.email,
        actor_client_id=actor.client_id,
    )
    submission.verifications.append(row)

    # The note stays out of the trail: it is free text about a person, and the
    # trail is readable by every tier that can read the queue. So do the digests:
    # the count says what the trail needs.
    detail = f"method={method.value}"
    if document_id is not None:
        detail = f"{detail} document={document_id}"
    if digests:
        detail = f"{detail} evidence={len(digests)}"
    if previous is not None:
        detail = f"{detail} supersedes={previous.id}"
    if renewal:
        action = "verification_renewed"
    elif previous is not None:
        action = "verification_superseded"
    else:
        action = "verification_recorded"
    audit_service.record(
        db,
        action=action,
        entity_type="submission",
        entity_id=str(submission.id),
        actor=actor,
        rec_slug=slug,
        ip=ip,
        detail=detail,
    )
    await db.commit()
    await db.refresh(row)
    return row


def _checked_evidence(evidence: list[dict[str, str]] | None) -> list[dict[str, str]]:
    if not evidence:
        raise VerificationError(
            "An offline-with-evidence verification needs the evidence file the community "
            "keeps (it is hashed, not stored)"
        )
    if len(evidence) > MAX_EVIDENCE:
        raise VerificationError(f"At most {MAX_EVIDENCE} evidence files per verification")
    out: list[dict[str, str]] = []
    for item in evidence:
        kind = str(item.get("kind") or "")
        digest = str(item.get("sha256") or "").lower()
        if kind not in key_assertion.EVIDENCE_KIND_VALUES:
            raise VerificationError(f"Unknown evidence kind {kind!r}")
        if not _SHA256.match(digest):
            raise VerificationError("An evidence digest is a sha256 in hex")
        out.append({"kind": kind, "sha256": digest})
    return out


# --- retention --------------------------------------------------------------------


def _years_after(moment: datetime, years: int) -> datetime:
    try:
        return moment.replace(year=moment.year + years)
    except ValueError:  # 29 February
        return moment.replace(year=moment.year + years, day=28)


def retain_on_erasure(submission: Submission, *, now: datetime | None = None) -> int:
    """Keep, past the submission's erasure, every verification that backs a grant.

    A row a holder accepted an assertion from (``last_asserted_at``) is the
    community's record of what it asserted, on what evidence: it is detached from
    the submission, its free-text note is cleared, and it is kept until the
    retention period after the later of the erasure and the last assertion
    (`ASSERTION_RETENTION_YEARS`, GDPR Art. 17(3)(e)). Every other row goes with
    the submission, as before.

    Call before deleting the submission. Returns how many rows were kept.
    """
    moment = now or datetime.now(UTC)
    kept = 0
    for row in list(submission.verifications):
        if row.last_asserted_at is None:
            continue
        last = row.last_asserted_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        until = _years_after(max(moment, last), settings.assertion_retention_years)
        row.retain_until = max(row.retain_until or until, until)
        row.note = None
        submission.verifications.remove(row)
        row.submission_id = None
        kept += 1
    return kept


async def purge_expired(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Delete the rows kept past an erasure whose retention period has ended."""
    result = await db.execute(
        delete(SubmissionVerification).where(
            SubmissionVerification.submission_id.is_(None),
            SubmissionVerification.retain_until.is_not(None),
            SubmissionVerification.retain_until < (now or datetime.now(UTC)),
        )
    )
    return int(result.rowcount or 0)
