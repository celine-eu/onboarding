import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.api.submissions import SESSION_TTL_SECONDS
from celine.onboarding.models.database import get_db
from celine.onboarding.models.document import DocumentType
from celine.onboarding.models.schemas import DocumentRead
from celine.onboarding.services import document_service, submission_service

router = APIRouter(tags=["documents"])


async def _check_submission_live(submission_id: uuid.UUID, db: AsyncSession):
    submission = await submission_service.get_submission(db, submission_id)
    if not submission:
        raise HTTPException(404, "Submission not found")
    if submission.created_at:
        age = (datetime.now(timezone.utc) - submission.created_at).total_seconds()
        if age > SESSION_TTL_SECONDS:
            raise HTTPException(410, "Session expired")
    return submission


@router.post(
    "/submissions/{submission_id}/documents",
    response_model=DocumentRead,
    status_code=201,
)
async def upload_document(
    submission_id: uuid.UUID,
    file: UploadFile,
    doc_type: DocumentType = DocumentType.UTILITY_BILL,
    db: AsyncSession = Depends(get_db),
):
    await _check_submission_live(submission_id, db)
    try:
        return await document_service.save_document(db, submission_id, file, doc_type)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get(
    "/submissions/{submission_id}/documents",
    response_model=list[DocumentRead],
)
async def list_documents(
    submission_id: uuid.UUID, db: AsyncSession = Depends(get_db)
):
    await _check_submission_live(submission_id, db)
    return await document_service.list_documents(db, submission_id)
