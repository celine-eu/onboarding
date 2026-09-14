"""Recording how the REC verified a participant, before approval.

The community verifies on its own responsibility — offline, or by confirming a
document stored on the submission — and records it here. Approval refuses
without one (`services/review.py`). Recording is its own audited action, so the
trail says who vouched for the person and when, separately from who approved.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.models.submission import Submission, SubmissionStatus
from celine.onboarding.models.verification import SubmissionVerification, VerificationMethod
from celine.onboarding.services import audit_service, document_service
from celine.onboarding.services.audit_service import Actor

#: A verification is evidence for a pending decision. Once the submission is
#: approved or rejected, correcting it would change what the decision rested on
#: after the fact — and an approved member's credential and registry entry already
#: carry the method — so it is refused there.
RECORDABLE = frozenset({SubmissionStatus.SUBMITTED, SubmissionStatus.UNDER_REVIEW})


class VerificationError(ValueError):
    """The verification cannot be recorded as asked."""


async def record(
    db: AsyncSession,
    submission: Submission,
    *,
    method: VerificationMethod,
    actor: Actor,
    document_id: uuid.UUID | None = None,
    note: str | None = None,
    ip: str | None = None,
    rec_slug: str | None = None,
) -> SubmissionVerification:
    if submission.status not in RECORDABLE:
        raise VerificationError(
            f"A verification can be recorded only while a submission is submitted or "
            f"under review, not {submission.status.value}"
        )

    if method is VerificationMethod.UPLOADED_DOCUMENT:
        if document_id is None:
            raise VerificationError("An uploaded-document verification must name the document")
        document = await document_service.get_document(db, document_id)
        # Same answer for "no such document" and "another submission's document":
        # which documents exist elsewhere is not this caller's business.
        if not document or document.submission_id != submission.id:
            raise VerificationError("The document is not stored on this submission")
    elif document_id is not None:
        raise VerificationError("An offline verification names no document")

    previous = submission.verification
    row = SubmissionVerification(
        # Set here rather than left to the column defaults, which apply only at
        # flush: the caller reads both straight back, and `created_at` is what
        # orders the history.
        id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        method=method.value,
        document_id=document_id,
        note=(note or "").strip() or None,
        actor_type=actor.type,
        actor_sub=actor.sub,
        actor_email=actor.email,
        actor_client_id=actor.client_id,
    )
    submission.verifications.append(row)

    # The note stays out of the trail: it is free text about a person, and the
    # trail is readable by every tier that can read the queue.
    detail = f"method={method.value}"
    if document_id is not None:
        detail = f"{detail} document={document_id}"
    if previous is not None:
        detail = f"{detail} supersedes={previous.id}"
    audit_service.record(
        db,
        action="verification_superseded" if previous is not None else "verification_recorded",
        entity_type="submission",
        entity_id=str(submission.id),
        actor=actor,
        rec_slug=rec_slug or submission.rec_slug,
        ip=ip,
        detail=detail,
    )
    await db.commit()
    await db.refresh(row)
    return row
