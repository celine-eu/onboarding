"""An operator's corrections of a participant's POD, names, email, fiscal code
and supply address.

From `submitted` on these six fields change only here (the admin `PATCH` refuses
them): each correction is a revision with its evidence and note, recorded beside
the value it replaced. See `services/revision.py`.

Recording needs `submissions.revise`, granted where `submissions.review` is: the
operator vouches for the new value. Reading the history needs `submissions.read`,
with the POD masked unless `reveal` is asked for, as on the submission itself.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from celine.sdk.auth import JwtUser
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from celine.onboarding.api.admin.deps import (
    ActorDep,
    DbDep,
    IpDep,
    RecDep,
    organization_of,
    require,
)
from celine.onboarding.api.admin.masking import mask_value
from celine.onboarding.api.admin.submissions import _owned_submission
from celine.onboarding.models.schemas import RevisionCreate, RevisionRead
from celine.onboarding.security.policy import Capability, get_policy
from celine.onboarding.services import audit_service, propagation, revision

router = APIRouter(tags=["admin"])

ReadDep = Annotated[JwtUser, Depends(require(Capability.SUBMISSIONS_READ))]
ReviseDep = Annotated[JwtUser, Depends(require(Capability.SUBMISSIONS_REVISE))]

#: Revised fields masked like the submission's own (`masking.MASKED_FIELDS`).
_MASKED = frozenset(
    {revision.RevisableField.POD_CODE.value, revision.RevisableField.FISCAL_CODE.value}
)


def _render(row, *, reveal: bool) -> RevisionRead:
    model = RevisionRead.model_validate(row)
    if reveal or model.field not in _MASKED:
        return model
    return model.model_copy(
        update={
            "previous_value": mask_value(model.previous_value),
            "new_value": mask_value(model.new_value),
        }
    )


@router.get(
    "/{rec_slug}/submissions/{submission_id}/revisions",
    response_model=list[RevisionRead],
)
async def list_revisions(
    submission_id: uuid.UUID,
    user: ReadDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
    reveal: bool = Query(
        False,
        description="Unmask the POD and fiscal code values. Requires `submissions.reveal`, and is "
        "recorded in the audit trail.",
    ),
):
    """Every revision, oldest first; per field, the last is in force."""
    if reveal:
        decision = get_policy().allow(
            user, Capability.SUBMISSIONS_REVEAL, organization=organization_of(rec_slug)
        )
        if not decision.allowed:
            raise HTTPException(
                403,
                f"Revealing the POD requires the reveal capability: "
                f"{decision.reason or 'access denied'}",
            )
    submission = await _owned_submission(db, submission_id, rec_slug)
    if reveal:
        await audit_service.record_and_commit(
            db,
            action="reveal",
            entity_type="submission",
            entity_id=str(submission_id),
            actor=actor,
            rec_slug=rec_slug,
            ip=ip,
            detail="revisions: fiscal_code, pod_code unmasked",
        )
    return [_render(row, reveal=reveal) for row in submission.revisions]


@router.post(
    "/{rec_slug}/submissions/{submission_id}/revisions",
    response_model=RevisionRead,
    status_code=201,
)
async def record_revision(
    submission_id: uuid.UUID,
    body: RevisionCreate,
    _: ReviseDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
):
    """Correct one field; the submission's column takes the new value.

    409 unless the submission is submitted, under review or approved; 422 for a
    value the field refuses, a missing note, the value already held, or a document
    that is missing, not on this submission, or given with `offline`. The POD is
    masked in the answer, like everywhere else.

    After approval the corrected value is then propagated (`services/propagation`);
    each step's state is in the answer's `steps`, a failure included. The revision
    stands whatever they do.
    """
    submission = await _owned_submission(db, submission_id, rec_slug)
    try:
        outcome = await revision.record(
            db,
            submission,
            field=revision.RevisableField(body.field),
            value=body.value,
            method=revision.RevisionMethod(body.method.value),
            document_id=body.document_id,
            note=body.note,
            actor=actor,
            policy=revision.OPERATOR,
            ip=ip,
            rec_slug=rec_slug,
        )
    except revision.RevisionStatusError as exc:
        raise HTTPException(409, str(exc)) from exc
    except revision.RevisionError as exc:
        raise HTTPException(422, str(exc)) from exc
    # After approval, carry the value to every system holding a copy. A step that
    # fails is recorded on its row and retried from there; the revision stands.
    await propagation.start(db, outcome)
    return _render(outcome.revision, reveal=False)


class RetryRequest(BaseModel):
    step: revision.PropagationStep | None = None


@router.post(
    "/{rec_slug}/submissions/{submission_id}/revisions/{revision_id}/retry",
    response_model=RevisionRead,
)
async def retry_revision(
    submission_id: uuid.UUID,
    revision_id: uuid.UUID,
    body: RetryRequest,
    _: ReviseDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
):
    """Re-run a revision's unfinished propagation steps, or one named step.

    404 for a revision that is not this submission's; 409 for one recorded before
    approval, which has nothing to propagate.
    """
    submission = await _owned_submission(db, submission_id, rec_slug)
    row = next((r for r in submission.revisions if r.id == revision_id), None)
    if row is None:
        raise HTTPException(404, "Revision not found")
    if not row.steps:
        raise HTTPException(409, "This revision was recorded before approval: nothing to propagate")
    await propagation.run(db, submission, row, only=body.step)
    audit_service.record(
        db,
        action="revision_retry",
        entity_type="submission",
        entity_id=str(submission_id),
        actor=actor,
        rec_slug=rec_slug,
        ip=ip,
        detail=f"revision={row.id} step={body.step.value if body.step else 'all'}",
    )
    await db.commit()
    return _render(row, reveal=False)
