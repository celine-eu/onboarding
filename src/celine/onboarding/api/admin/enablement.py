"""What approval did, and how to repair it.

Before this the only recoverable step was the consent share. A registry failure
was a 422 whose only answer was pressing Approve again, re-running every step and
hoping the ones that had worked were idempotent.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from celine.sdk.auth import JwtUser
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from celine.onboarding.api.admin.deps import ActorDep, DbDep, IpDep, RecDep, require
from celine.onboarding.api.admin.submissions import _owned_submission
from celine.onboarding.models.enablement import SubmissionEnablementStep
from celine.onboarding.security.policy import Capability
from celine.onboarding.services import audit_service, enablement

router = APIRouter(tags=["admin"])

RetryDep = Annotated[JwtUser, Depends(require(Capability.ENABLEMENT_RETRY))]
RevokeDep = Annotated[JwtUser, Depends(require(Capability.ENABLEMENT_REVOKE))]
ReadDep = Annotated[JwtUser, Depends(require(Capability.SUBMISSIONS_READ))]


class StepRead(BaseModel):
    step: str
    label: str
    fail_closed: bool
    status: str
    external_ref: str | None
    attempts: int
    last_error: str | None
    detail: str | None
    started_at: datetime | None
    completed_at: datetime | None


class EnablementRead(BaseModel):
    submission_id: uuid.UUID
    # not_started | partial | complete | failed
    state: str
    steps: list[StepRead]


class RetryRequest(BaseModel):
    step: str | None = Field(
        None,
        description="One step to re-run. Omit to re-run every step that is not "
        "already succeeded or skipped.",
    )


def _render(submission_id: uuid.UUID, rows: dict[str, SubmissionEnablementStep]) -> EnablementRead:
    """Always the full pipeline, in order — including steps with no row yet.

    A console that only showed rows that exist would show a shorter pipeline for a
    submission nobody has tried to enable, which reads as "fewer things to do"
    rather than "not started".
    """
    steps = []
    for spec in enablement.PIPELINE:
        row = rows.get(spec.step)
        steps.append(
            StepRead(
                step=spec.step,
                label=spec.label,
                fail_closed=spec.fail_closed,
                status=row.status if row else "pending",
                external_ref=row.external_ref if row else None,
                attempts=row.attempts if row else 0,
                last_error=row.last_error if row else None,
                detail=row.detail if row else None,
                started_at=row.started_at if row else None,
                completed_at=row.completed_at if row else None,
            )
        )
    return EnablementRead(submission_id=submission_id, state=enablement.state_of(rows), steps=steps)


@router.get("/{rec_slug}/submissions/{submission_id}/enablement", response_model=EnablementRead)
async def get_enablement(
    submission_id: uuid.UUID,
    _: ReadDep,
    db: DbDep,
    rec_slug: RecDep,
):
    await _owned_submission(db, submission_id, rec_slug)
    rows = await enablement.load_steps(db, submission_id)
    return _render(submission_id, rows)


@router.post(
    "/{rec_slug}/submissions/{submission_id}/enablement/retry",
    response_model=EnablementRead,
)
async def retry_enablement(
    submission_id: uuid.UUID,
    body: RetryRequest,
    _: RetryDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
):
    """Re-run failed steps. Never raises on failure — the step rows are the answer.

    The submission is already approved, so unlike approval there is no decision to
    block: the operator asked to repair, and either it worked or the row now says
    why it did not.
    """
    submission = await _owned_submission(db, submission_id, rec_slug)

    try:
        rows = await enablement.retry(db, submission, step=body.step)
    except KeyError as exc:
        raise HTTPException(422, str(exc))

    await audit_service.record_and_commit(
        db,
        action="enablement_retry",
        entity_type="submission",
        entity_id=str(submission_id),
        actor=actor,
        rec_slug=rec_slug,
        ip=ip,
        detail=f"step={body.step or 'all'} state={enablement.state_of(rows)}",
    )
    return _render(submission_id, rows)


@router.post(
    "/{rec_slug}/submissions/{submission_id}/enablement/revoke",
    response_model=EnablementRead,
)
async def revoke_enablement(
    submission_id: uuid.UUID,
    _: RevokeDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
):
    """Undo enablement in reverse: credential, membership, registry, login.

    Best-effort per step and recorded per step. A revocation that fails half way
    must leave a record of what is still out there — that record is the only way
    anybody finds the rest.

    The standing sharing consent is deliberately **not** revoked here: withdrawal
    is the data subject's own act, authenticated with their own credential, and
    onboarding holds no credential to make it on their behalf.
    """
    submission = await _owned_submission(db, submission_id, rec_slug)
    rows = await enablement.revoke(db, submission)

    await audit_service.record_and_commit(
        db,
        action="enablement_revoke",
        entity_type="submission",
        entity_id=str(submission_id),
        actor=actor,
        rec_slug=rec_slug,
        ip=ip,
        detail=f"state={enablement.state_of(rows)}",
    )
    return _render(submission_id, rows)
