"""The REC's verification of a participant, recorded before approval.

Approval refuses until one is recorded (`services/review.py`). Recording needs the
same capability as approving, `submissions.review`: vouching for a person is part
of the decision, not a data correction an `editors` operator can make.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from celine.sdk.auth import JwtUser
from fastapi import APIRouter, Depends, HTTPException

from celine.onboarding.api.admin.deps import ActorDep, DbDep, IpDep, RecDep, require
from celine.onboarding.api.admin.submissions import _owned_submission
from celine.onboarding.models.schemas import VerificationCreate, VerificationRead
from celine.onboarding.security.policy import Capability
from celine.onboarding.services import verification

router = APIRouter(tags=["admin"])

ReadDep = Annotated[JwtUser, Depends(require(Capability.SUBMISSIONS_READ))]
ReviewDep = Annotated[JwtUser, Depends(require(Capability.SUBMISSIONS_REVIEW))]


@router.get(
    "/{rec_slug}/submissions/{submission_id}/verifications",
    response_model=list[VerificationRead],
)
async def list_verifications(submission_id: uuid.UUID, _: ReadDep, db: DbDep, rec_slug: RecDep):
    """Every verification recorded, oldest first. The last is in force."""
    submission = await _owned_submission(db, submission_id, rec_slug)
    return submission.verifications


@router.post(
    "/{rec_slug}/submissions/{submission_id}/verifications",
    response_model=VerificationRead,
    status_code=201,
)
async def record_verification(
    submission_id: uuid.UUID,
    body: VerificationCreate,
    _: ReviewDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
):
    """Record how the REC verified the person; supersedes any earlier one.

    409 once the submission is approved or rejected; 422 for a document that is
    missing, not on this submission, or given with `offline`.
    """
    submission = await _owned_submission(db, submission_id, rec_slug)
    if submission.status not in verification.RECORDABLE:
        raise HTTPException(
            409,
            f"A verification can be recorded only while a submission is submitted or "
            f"under review, not {submission.status.value}",
        )
    try:
        return await verification.record(
            db,
            submission,
            method=body.method,
            document_id=body.document_id,
            note=body.note,
            actor=actor,
            ip=ip,
            rec_slug=rec_slug,
        )
    except verification.VerificationError as exc:
        raise HTTPException(422, str(exc)) from exc
