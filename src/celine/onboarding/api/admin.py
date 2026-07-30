import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.api.deps import require_rec_admin, valid_rec_slug
from celine.onboarding.config.settings import settings
from celine.onboarding.models.audit_log import AuditLog
from celine.onboarding.models.database import get_db
from celine.onboarding.models.schemas import AuditLogRead, SubmissionAdminRead, SubmissionUpdate
from celine.onboarding.services import audit_service, submission_service
from celine.onboarding.workflows.engine import InvalidTransition

router = APIRouter(tags=["admin"], dependencies=[Depends(require_rec_admin)])


def _client_ip(request: Request) -> str:
    return request.headers.get(
        "x-forwarded-for", request.client.host if request.client else "unknown"
    )


async def _audit(
    db: AsyncSession, *, action: str, entity_type: str, entity_id: str | None,
    ip: str, rec_slug: str | None = None, detail: str | None = None,
) -> None:
    # Attributed to `Actor.shared_token()`: this surface authenticates with the
    # deployment-wide ADMIN_TOKEN, so there is no identity to record. That is the
    # gap the actor columns exist to close, and it closes when the token goes.
    await audit_service.record_and_commit(
        db,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor=audit_service.Actor.shared_token(),
        rec_slug=rec_slug,
        ip=ip,
        detail=detail,
    )


@router.get("/submissions", response_model=list[SubmissionAdminRead])
async def list_submissions(
    request: Request,
    rec_slug: str = Depends(valid_rec_slug),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    result = await submission_service.list_submissions(db, rec_slug=rec_slug, skip=skip, limit=limit)
    await _audit(db, action="list", entity_type="submission", entity_id=None,
                 ip=_client_ip(request), rec_slug=rec_slug,
                 detail=f"skip={skip} limit={limit}")
    return result


@router.get("/submissions/{submission_id}", response_model=SubmissionAdminRead)
async def get_submission(
    submission_id: uuid.UUID,
    request: Request,
    rec_slug: str = Depends(valid_rec_slug),
    db: AsyncSession = Depends(get_db),
):
    submission = await submission_service.get_submission(db, submission_id)
    if not submission:
        raise HTTPException(404, "Submission not found")
    if submission.rec_slug != rec_slug:
        raise HTTPException(404, "Submission not found")
    await _audit(db, action="view", entity_type="submission",
                 entity_id=str(submission_id), ip=_client_ip(request), rec_slug=rec_slug)
    return submission


@router.patch("/submissions/{submission_id}", response_model=SubmissionAdminRead)
async def update_submission(
    submission_id: uuid.UUID,
    data: SubmissionUpdate,
    request: Request,
    background_tasks: BackgroundTasks,
    rec_slug: str = Depends(valid_rec_slug),
    db: AsyncSession = Depends(get_db),
):
    submission = await submission_service.get_submission(db, submission_id)
    if not submission:
        raise HTTPException(404, "Submission not found")
    if submission.rec_slug != rec_slug:
        raise HTTPException(404, "Submission not found")
    fields = ", ".join(data.model_dump(exclude_unset=True).keys())
    try:
        result = await submission_service.update_submission(
            db, submission, data, background_tasks=background_tasks
        )
    except (ValueError, InvalidTransition) as e:
        raise HTTPException(422, str(e))
    await _audit(db, action="update", entity_type="submission",
                 entity_id=str(submission_id), ip=_client_ip(request),
                 rec_slug=rec_slug, detail=f"fields: {fields}")
    return result


@router.delete("/submissions/{submission_id}", status_code=204)
async def delete_submission(
    submission_id: uuid.UUID,
    request: Request,
    rec_slug: str = Depends(valid_rec_slug),
    db: AsyncSession = Depends(get_db),
):
    submission = await submission_service.get_submission(db, submission_id)
    if not submission:
        raise HTTPException(404, "Submission not found")
    if submission.rec_slug != rec_slug:
        raise HTTPException(404, "Submission not found")

    ref = submission.ref
    for doc in submission.documents:
        fpath = Path(settings.data_dir) / doc.file_path
        fpath.unlink(missing_ok=True)

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

    await _audit(db, action="delete", entity_type="submission",
                 entity_id=str(submission_id), ip=_client_ip(request),
                 rec_slug=rec_slug, detail=f"ref={ref} — GDPR erasure")


@router.post("/submissions/{submission_id}/retry-share", response_model=SubmissionAdminRead)
async def retry_share(
    submission_id: uuid.UUID,
    request: Request,
    rec_slug: str = Depends(valid_rec_slug),
    db: AsyncSession = Depends(get_db),
):
    """Re-attempt data-sharing provisioning for an already-provisioned identity.

    A failed share does not fail approval (§3.5), so a submission can be approved
    with ``share_provisioned = false``. This retries just the connector push;
    idempotent, and raises 422 if the connector rejects an offer.
    """
    from celine.onboarding.services.dataspace_identity import provision_user_shares

    submission = await submission_service.get_submission(db, submission_id)
    if not submission:
        raise HTTPException(404, "Submission not found")
    if submission.rec_slug != rec_slug:
        raise HTTPException(404, "Submission not found")

    try:
        await provision_user_shares(submission, raise_on_error=True)
    except ValueError as e:
        raise HTTPException(422, str(e))
    await db.commit()

    await _audit(db, action="retry_share", entity_type="submission",
                 entity_id=str(submission_id), ip=_client_ip(request),
                 rec_slug=rec_slug,
                 detail=f"share_provisioned={submission.share_provisioned}")
    return submission


@router.get("/submissions/{submission_id}/pdf")
async def download_submission_pdf(
    submission_id: uuid.UUID,
    request: Request,
    rec_slug: str = Depends(valid_rec_slug),
    db: AsyncSession = Depends(get_db),
):
    from celine.onboarding.services.pdf_service import generate_submission_pdf

    submission = await submission_service.get_submission(db, submission_id)
    if not submission:
        raise HTTPException(404, "Submission not found")
    if submission.rec_slug != rec_slug:
        raise HTTPException(404, "Submission not found")

    await _audit(db, action="download_pdf", entity_type="submission",
                 entity_id=str(submission_id), ip=_client_ip(request), rec_slug=rec_slug)

    pdf_bytes = generate_submission_pdf(submission)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{submission.ref}-summary.pdf"'},
    )


@router.get("/audit-logs", response_model=list[AuditLogRead])
async def list_audit_logs(
    rec_slug: str = Depends(valid_rec_slug),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """One community's trail.

    This used to return every community's — the endpoint sits under
    `/api/{rec}/admin` but ignored the slug, so any token holder read the whole
    deployment's history. Rows written before `rec_slug` existed and not
    recoverable by the 0009 backfill are excluded: they name no community, and
    showing them under an arbitrary one would be inventing the fact.
    """
    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.rec_slug == rec_slug)
        .order_by(AuditLog.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().all())
