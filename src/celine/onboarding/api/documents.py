import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.api.submissions import _get_live_submission
from celine.onboarding.models.database import get_db
from celine.onboarding.models.document import DocumentType
from celine.onboarding.models.schemas import DocumentRead
from celine.onboarding.services import document_service

router = APIRouter(tags=["documents"])


@router.post(
    "/submissions/{submission_id}/documents",
    response_model=DocumentRead,
    status_code=201,
)
async def upload_document(
    submission_id: uuid.UUID,
    request: Request,
    file: UploadFile,
    doc_type: DocumentType = DocumentType.UTILITY_BILL,
    db: AsyncSession = Depends(get_db),
):
    await _get_live_submission(submission_id, request, db)
    try:
        return await document_service.save_document(db, submission_id, file, doc_type)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get(
    "/submissions/{submission_id}/documents",
    response_model=list[DocumentRead],
)
async def list_documents(
    submission_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    await _get_live_submission(submission_id, request, db)
    return await document_service.list_documents(db, submission_id)
