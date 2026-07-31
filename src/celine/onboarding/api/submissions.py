import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.config.settings import settings
from celine.onboarding.api.deps import limiter, valid_rec_slug
from celine.onboarding.models.database import get_db
from celine.onboarding.models.schemas import (
    ConsentCreate,
    SubmissionCreatedRead,
    SubmissionRead,
    SubmissionUpdate,
)
from celine.onboarding.models.submission import Submission
from celine.onboarding.services import submission_service
from celine.onboarding.workflows.engine import InvalidTransition

router = APIRouter(prefix="/submissions", tags=["submissions"])

SESSION_TTL_SECONDS = 600


async def _get_live_submission(
    submission_id: uuid.UUID,
    request: Request,
    *,
    rec_slug: str | None = None,
    db: AsyncSession,
) -> Submission:
    submission = await submission_service.get_submission(db, submission_id)
    if not submission:
        raise HTTPException(404, "Submission not found")

    if rec_slug is not None and submission.rec_slug != rec_slug:
        raise HTTPException(403, "Submission does not belong to this REC")

    token = request.headers.get("x-session-token", "")
    if not token or not secrets.compare_digest(token, submission.session_token):
        raise HTTPException(403, "Invalid session token")

    now = datetime.now(timezone.utc)
    anchor = submission.last_active_at or submission.created_at
    if anchor and (now - anchor).total_seconds() > SESSION_TTL_SECONDS:
        raise HTTPException(410, "Session expired. Please start a new submission.")

    submission.last_active_at = now
    await db.commit()
    # The commit issues an UPDATE, so server-computed columns (updated_at has
    # onupdate=func.now()) are expired. Refresh inside the async context, or
    # response serialization would lazy-load them outside the greenlet and 500
    # with MissingGreenlet.
    await db.refresh(submission)

    return submission


@router.post("", response_model=SubmissionCreatedRead, status_code=201)
@limiter.limit(lambda: settings.rate_limit_submissions)
async def create_submission(
    request: Request,
    data: ConsentCreate,
    rec_slug: str = Depends(valid_rec_slug),
    db: AsyncSession = Depends(get_db),
):
    client_ip = request.headers.get(
        "x-forwarded-for", request.client.host if request.client else "unknown"
    )
    submission = await submission_service.create_from_consent(db, data, client_ip, rec_slug)
    return submission


@router.get("/{submission_id}", response_model=SubmissionRead)
async def get_submission(
    submission_id: uuid.UUID,
    request: Request,
    rec_slug: str = Depends(valid_rec_slug),
    db: AsyncSession = Depends(get_db),
):
    return await _get_live_submission(submission_id, request, rec_slug=rec_slug, db=db)


@router.patch("/{submission_id}", response_model=SubmissionRead)
async def update_submission(
    submission_id: uuid.UUID,
    data: SubmissionUpdate,
    request: Request,
    background_tasks: BackgroundTasks,
    rec_slug: str = Depends(valid_rec_slug),
    db: AsyncSession = Depends(get_db),
):
    submission = await _get_live_submission(submission_id, request, rec_slug=rec_slug, db=db)
    try:
        return await submission_service.update_submission(
            db, submission, data, background_tasks=background_tasks
        )
    except (ValueError, InvalidTransition) as e:
        raise HTTPException(422, str(e))


@router.get("/{submission_id}/pdf")
@limiter.limit(lambda: settings.rate_limit_pdf)
async def download_submission_pdf(
    submission_id: uuid.UUID,
    request: Request,
    rec_slug: str = Depends(valid_rec_slug),
    db: AsyncSession = Depends(get_db),
):
    from celine.onboarding.services.pdf_service import generate_submission_pdf

    submission = await _get_live_submission(submission_id, request, rec_slug=rec_slug, db=db)
    pdf_bytes = generate_submission_pdf(submission)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{submission.ref}-summary.pdf"'},
    )
