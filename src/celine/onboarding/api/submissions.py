import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.api.deps import limiter
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
    submission_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> Submission:
    submission = await submission_service.get_submission(db, submission_id)
    if not submission:
        raise HTTPException(404, "Submission not found")

    token = request.headers.get("x-session-token", "")
    if not token or token != submission.session_token:
        raise HTTPException(403, "Invalid session token")

    now = datetime.now(timezone.utc)
    anchor = submission.last_active_at or submission.created_at
    if anchor and (now - anchor).total_seconds() > SESSION_TTL_SECONDS:
        raise HTTPException(410, "Session expired. Please start a new submission.")

    submission.last_active_at = now
    await db.commit()

    return submission


@router.post("", response_model=SubmissionCreatedRead, status_code=201)
@limiter.limit("20/hour")
async def create_submission(
    request: Request,
    data: ConsentCreate,
    db: AsyncSession = Depends(get_db),
):
    client_ip = request.headers.get(
        "x-forwarded-for", request.client.host if request.client else "unknown"
    )
    submission = await submission_service.create_from_consent(db, data, client_ip)
    return submission


@router.get("/{submission_id}", response_model=SubmissionRead)
async def get_submission(submission: Submission = Depends(_get_live_submission)):
    return submission


@router.patch("/{submission_id}", response_model=SubmissionRead)
async def update_submission(
    data: SubmissionUpdate,
    submission: Submission = Depends(_get_live_submission),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await submission_service.update_submission(db, submission, data)
    except (ValueError, InvalidTransition) as e:
        raise HTTPException(422, str(e))


@router.get("/{submission_id}/pdf")
@limiter.limit("5/minute")
async def download_submission_pdf(
    request: Request,
    submission: Submission = Depends(_get_live_submission),
):
    from celine.onboarding.services.pdf_service import generate_submission_pdf

    pdf_bytes = generate_submission_pdf(submission)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{submission.ref}-summary.pdf"'},
    )
