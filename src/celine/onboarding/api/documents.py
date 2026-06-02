import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.models.database import get_db
from celine.onboarding.models.document import DocumentType
from celine.onboarding.models.schemas import DocumentRead
from celine.onboarding.services import document_service, submission_service

router = APIRouter(tags=["documents"])


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
    submission = await submission_service.get_submission(db, submission_id)
    if not submission:
        raise HTTPException(404, "Submission not found")

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
    return await document_service.list_documents(db, submission_id)


@router.get("/documents/{document_id}/download")
async def download_document(
    document_id: uuid.UUID, db: AsyncSession = Depends(get_db)
):
    document = await document_service.get_document(db, document_id)
    if not document:
        raise HTTPException(404, "Document not found")

    file_path = document_service.get_file_path(document)
    if not file_path.exists():
        raise HTTPException(404, "File not found on disk")

    return FileResponse(
        file_path,
        media_type=document.mime_type,
        filename=document.original_filename,
    )
