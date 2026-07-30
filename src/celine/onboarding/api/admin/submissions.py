"""Operator access to submissions.

Moved from `api/admin.py` (mounted at `/api/{rec}/admin`) with two changes: the
shared bearer token is gone, and each endpoint names the capability it needs
instead of all six sharing one gate. So an `editors` operator can correct a
misread fiscal code but cannot approve anybody, and only an `admins` one can
erase.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated

from celine.sdk.auth import JwtUser
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from celine.onboarding.api.admin.deps import (
    ActorDep,
    DbDep,
    IpDep,
    RecDep,
    organization_of,
    require,
)
from celine.onboarding.api.admin.masking import MASKED_FIELDS, mask_value
from celine.onboarding.config.settings import settings
from celine.onboarding.models.schemas import SubmissionAdminRead, SubmissionUpdate
from celine.onboarding.models.submission import SubmissionStatus
from celine.onboarding.security.policy import Capability, get_policy
from celine.onboarding.services import audit_service, review, submission_service
from celine.onboarding.services.enablement import EnablementFailed
from celine.onboarding.workflows.engine import InvalidTransition

router = APIRouter(tags=["admin"])

ReadDep = Annotated[JwtUser, Depends(require(Capability.SUBMISSIONS_READ))]
WriteDep = Annotated[JwtUser, Depends(require(Capability.SUBMISSIONS_WRITE))]
PurgeDep = Annotated[JwtUser, Depends(require(Capability.SUBMISSIONS_PURGE))]
RetryDep = Annotated[JwtUser, Depends(require(Capability.ENABLEMENT_RETRY))]
ReviewDep = Annotated[JwtUser, Depends(require(Capability.SUBMISSIONS_REVIEW))]


def _read(submission, *, reveal: bool = False) -> SubmissionAdminRead:
    """Serialise a submission, masking the identifiers unless asked otherwise.

    `fiscal_code` and `pod_code` are encrypted at rest; handing them back in clear
    to every operator would mean the encryption protects the backup tape and
    nothing else. See `api/admin/masking.py`.
    """
    model = SubmissionAdminRead.model_validate(submission)
    if reveal:
        return model
    return model.model_copy(
        update={field: mask_value(getattr(model, field)) for field in MASKED_FIELDS}
    )


async def _owned_submission(db, submission_id: uuid.UUID, rec_slug: str):
    """Load a submission, 404 unless it belongs to the REC in the path.

    404 rather than 403 on the ownership mismatch: whether a submission exists in
    a community the caller cannot see is itself information.
    """
    submission = await submission_service.get_submission(db, submission_id)
    if not submission or submission.rec_slug != rec_slug:
        raise HTTPException(404, "Submission not found")
    return submission


@router.get("/{rec_slug}/submissions", response_model=list[SubmissionAdminRead])
async def list_submissions(
    _: ReadDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
    response: Response,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    status: SubmissionStatus | None = Query(None),
    ref: str | None = Query(
        None,
        max_length=40,
        description="Substring of the submission reference. Deliberately the only "
        "searchable field: everything else is encrypted with a non-deterministic "
        "IV, so there is no ciphertext to match against.",
    ),
    created_from: datetime | None = Query(None),
    created_to: datetime | None = Query(None),
):
    """One page of the queue, always masked.

    Reveal is a per-record act on the detail endpoint, not something a list can
    do wholesale.
    """
    filters = {
        "status": status,
        "ref": ref,
        "created_from": created_from,
        "created_to": created_to,
    }
    result = await submission_service.list_submissions(
        db, rec_slug=rec_slug, skip=skip, limit=limit, **filters
    )
    # Without the total the console cannot paginate — it has no way to tell a
    # full last page from a page that merely happens to be full.
    total = await submission_service.count_submissions(db, rec_slug=rec_slug, **filters)
    response.headers["X-Total-Count"] = str(total)

    await audit_service.record_and_commit(
        db,
        action="list",
        entity_type="submission",
        entity_id=None,
        actor=actor,
        rec_slug=rec_slug,
        ip=ip,
        detail=f"skip={skip} limit={limit} total={total}",
    )
    return [_read(s) for s in result]


@router.get("/{rec_slug}/submissions/{submission_id}", response_model=SubmissionAdminRead)
async def get_submission(
    submission_id: uuid.UUID,
    user: ReadDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
    reveal: bool = Query(
        False,
        description="Unmask the fiscal code and POD. Requires `submissions.reveal`, "
        "and is recorded in the audit trail.",
    ),
):
    """One submission, with the identifiers masked unless `reveal` is asked for.

    The point of the reveal capability is not that an operator must never see a
    fiscal code — sometimes they must, to resolve exactly the kind of mismatch
    review exists to catch — but that doing so is a deliberate act with their name
    on it.
    """
    if reveal:
        decision = get_policy().allow(
            user, Capability.SUBMISSIONS_REVEAL, organization=organization_of(rec_slug)
        )
        if not decision.allowed:
            raise HTTPException(
                403,
                f"Revealing the fiscal code and POD requires the reveal capability: "
                f"{decision.reason or 'access denied'}",
            )

    submission = await _owned_submission(db, submission_id, rec_slug)
    await audit_service.record_and_commit(
        db,
        action="reveal" if reveal else "view",
        entity_type="submission",
        entity_id=str(submission_id),
        actor=actor,
        rec_slug=rec_slug,
        ip=ip,
        detail="fiscal_code, pod_code unmasked" if reveal else None,
    )
    return _read(submission, reveal=reveal)


@router.patch("/{rec_slug}/submissions/{submission_id}", response_model=SubmissionAdminRead)
async def update_submission(
    submission_id: uuid.UUID,
    data: SubmissionUpdate,
    user: WriteDep,
    background_tasks: BackgroundTasks,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
):
    """Edit fields, and — for now — drive the state machine.

    A payload carrying `status` additionally requires `submissions.review`: an
    editor may correct a misread fiscal code, but approving somebody provisions a
    login, a registry member and a dataspace identity, which is a different
    decision. The transition moves to its own endpoint in B2, where it can also
    carry a rejection reason; the extra check is here so the distinction is
    enforced now rather than after the restructure.
    """
    if data.model_fields_set & {"status"}:
        decision = get_policy().allow(
            user, Capability.SUBMISSIONS_REVIEW, organization=organization_of(rec_slug)
        )
        if not decision.allowed:
            raise HTTPException(
                403,
                f"Changing status requires the review capability: "
                f"{decision.reason or 'access denied'}",
            )

    submission = await _owned_submission(db, submission_id, rec_slug)
    fields = ", ".join(sorted(data.model_dump(exclude_unset=True).keys()))
    try:
        result = await submission_service.update_submission(
            db, submission, data, background_tasks=background_tasks
        )
    except (ValueError, InvalidTransition) as exc:
        raise HTTPException(422, str(exc))

    await audit_service.record_and_commit(
        db,
        action="update",
        entity_type="submission",
        entity_id=str(submission_id),
        actor=actor,
        rec_slug=rec_slug,
        ip=ip,
        detail=f"fields: {fields}",
    )
    return _read(result)


class TransitionRequest(BaseModel):
    target: SubmissionStatus
    reason: str | None = Field(
        None,
        max_length=1000,
        description="Why. Required when rejecting — the participant is told, and "
        "an operator reopening the case months later needs to know.",
    )


@router.post(
    "/{rec_slug}/submissions/{submission_id}/transition",
    response_model=SubmissionAdminRead,
)
async def transition_submission(
    submission_id: uuid.UUID,
    body: TransitionRequest,
    _: ReviewDep,
    background_tasks: BackgroundTasks,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
):
    """Drive the review state machine.

    Split out of `PATCH` because the state machine is not a field: it has its own
    capability, its own preconditions, and a reason that a field update has
    nowhere to put.

    On approval the enablement pipeline runs first. A fail-closed step failing
    returns 422 and leaves the submission in review — but everything the pipeline
    *did* manage is committed, so the retry finishes the job instead of starting
    it again.
    """
    if body.target == SubmissionStatus.REJECTED and not (body.reason or "").strip():
        raise HTTPException(422, "A reason is required when rejecting a submission")

    submission = await _owned_submission(db, submission_id, rec_slug)
    try:
        result = await review.transition(
            db,
            submission,
            body.target,
            actor=actor,
            reason=body.reason,
            ip=ip,
            rec_slug=rec_slug,
            background_tasks=background_tasks,
        )
    except EnablementFailed as exc:
        raise HTTPException(422, str(exc))
    except (ValueError, InvalidTransition) as exc:
        raise HTTPException(422, str(exc))
    return _read(result)


@router.delete("/{rec_slug}/submissions/{submission_id}", status_code=204)
async def delete_submission(
    submission_id: uuid.UUID,
    _: PurgeDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
):
    """GDPR erasure: files from disk, rows from the database."""
    submission = await _owned_submission(db, submission_id, rec_slug)

    ref = submission.ref
    for doc in submission.documents:
        (Path(settings.data_dir) / doc.file_path).unlink(missing_ok=True)

    sub_dir = Path(settings.data_dir) / rec_slug / "submissions" / ref
    if sub_dir.is_dir():
        for f in sub_dir.iterdir():
            f.unlink(missing_ok=True)
        sub_dir.rmdir()

    # backward compat: also check old path
    old_dir = Path(settings.data_dir) / "submissions" / ref
    if old_dir.is_dir():
        for f in old_dir.iterdir():
            f.unlink(missing_ok=True)
        old_dir.rmdir()

    await db.delete(submission)
    await db.commit()

    await audit_service.record_and_commit(
        db,
        action="delete",
        entity_type="submission",
        entity_id=str(submission_id),
        actor=actor,
        rec_slug=rec_slug,
        ip=ip,
        detail=f"ref={ref} — GDPR erasure",
    )


@router.post(
    "/{rec_slug}/submissions/{submission_id}/retry-share",
    response_model=SubmissionAdminRead,
    deprecated=True,
)
async def retry_share(
    submission_id: uuid.UUID,
    _: RetryDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
):
    """Deprecated alias for `enablement/retry?step=dataspace_share`.

    Kept for one release because it is the only admin endpoint that existed before
    the console did. The enablement endpoint supersedes it: it records the attempt
    on the step row, so a repeated failure is visible as a count rather than as a
    422 the operator sees and nothing remembers.
    """
    from celine.onboarding.services.dataspace_identity import provision_user_shares

    submission = await _owned_submission(db, submission_id, rec_slug)

    try:
        await provision_user_shares(submission, raise_on_error=True)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    await db.commit()

    await audit_service.record_and_commit(
        db,
        action="retry_share",
        entity_type="submission",
        entity_id=str(submission_id),
        actor=actor,
        rec_slug=rec_slug,
        ip=ip,
        detail=f"share_provisioned={submission.share_provisioned}",
    )
    return _read(submission)


@router.get("/{rec_slug}/submissions/{submission_id}/pdf")
async def download_submission_pdf(
    submission_id: uuid.UUID,
    _: ReadDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
):
    from celine.onboarding.services.pdf_service import generate_submission_pdf

    submission = await _owned_submission(db, submission_id, rec_slug)

    await audit_service.record_and_commit(
        db,
        action="download_pdf",
        entity_type="submission",
        entity_id=str(submission_id),
        actor=actor,
        rec_slug=rec_slug,
        ip=ip,
    )

    pdf_bytes = generate_submission_pdf(submission)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{submission.ref}-summary.pdf"'},
    )
