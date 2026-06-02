import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.models.database import get_db
from celine.onboarding.models.schemas import ConsentCreate, SubmissionRead, SubmissionUpdate
from celine.onboarding.services import submission_service
from celine.onboarding.workflows.engine import InvalidTransition

router = APIRouter(prefix="/submissions", tags=["submissions"])


@router.post("", response_model=SubmissionRead, status_code=201)
async def create_submission(
    data: ConsentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    client_ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown")
    submission = await submission_service.create_from_consent(db, data, client_ip)
    return submission


@router.get("", response_model=list[SubmissionRead])
async def list_submissions(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    return await submission_service.list_submissions(db, skip=skip, limit=limit)


@router.get("/{submission_id}", response_model=SubmissionRead)
async def get_submission(
    submission_id: uuid.UUID, db: AsyncSession = Depends(get_db)
):
    submission = await submission_service.get_submission(db, submission_id)
    if not submission:
        raise HTTPException(404, "Submission not found")
    return submission


@router.patch("/{submission_id}", response_model=SubmissionRead)
async def update_submission(
    submission_id: uuid.UUID,
    data: SubmissionUpdate,
    db: AsyncSession = Depends(get_db),
):
    submission = await submission_service.get_submission(db, submission_id)
    if not submission:
        raise HTTPException(404, "Submission not found")
    try:
        return await submission_service.update_submission(db, submission, data)
    except (ValueError, InvalidTransition) as e:
        raise HTTPException(422, str(e))


@router.get("/{submission_id}/pdf")
async def download_submission_pdf(
    submission_id: uuid.UUID, db: AsyncSession = Depends(get_db)
):
    from datetime import datetime, timezone

    from celine.onboarding.services.pdf_service import generate_submission_pdf

    submission = await submission_service.get_submission(db, submission_id)
    if not submission:
        raise HTTPException(404, "Submission not found")

    if submission.created_at:
        age = (datetime.now(timezone.utc) - submission.created_at).total_seconds()
        if age > 600:
            raise HTTPException(410, "PDF download expired for privacy protection")

    pdf_bytes = generate_submission_pdf(submission)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{submission.ref}-summary.pdf"'},
    )
