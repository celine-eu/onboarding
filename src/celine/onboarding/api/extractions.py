import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.api.deps import limiter, require_session
from celine.onboarding.models.database import get_db
from celine.onboarding.models.schemas import ExtractionConfirm, ExtractionRead
from celine.onboarding.models.submission import Submission
from celine.onboarding.services import document_service, extraction_service

router = APIRouter(tags=["extractions"])

ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}


@router.post("/extract")
@limiter.limit("10/hour")
async def extract_from_upload(
    request: Request,
    files: Annotated[list[UploadFile], File()],
    _session: Submission = Depends(require_session),
):
    """Extract structured data from bill pages (images/PDFs)."""
    from celine.onboarding.extractors.openai_extractor import OpenAIExtractor

    pages = []
    for f in files:
        if f.content_type not in ALLOWED_MIME_TYPES:
            raise HTTPException(400, f"Unsupported file type: {f.content_type}")
        pages.append((await f.read(), f.content_type or "application/octet-stream"))

    extractor = OpenAIExtractor()
    extracted_data, _ = await extractor.extract_pages(pages)
    return extracted_data


@router.post("/extract-id")
@limiter.limit("10/hour")
async def extract_from_id_upload(
    request: Request,
    files: Annotated[list[UploadFile], File()],
    _session: Submission = Depends(require_session),
):
    """Extract structured data from ID card pages (images/PDFs)."""
    from celine.onboarding.extractors.openai_extractor import (
        ID_CARD_SYSTEM_PROMPT,
        ID_CARD_USER_PROMPT,
        OpenAIExtractor,
    )

    pages = []
    for f in files:
        if f.content_type not in ALLOWED_MIME_TYPES:
            raise HTTPException(400, f"Unsupported file type: {f.content_type}")
        pages.append((await f.read(), f.content_type or "application/octet-stream"))

    extractor = OpenAIExtractor()
    extracted_data, _ = await extractor.extract_pages(
        pages, system_prompt=ID_CARD_SYSTEM_PROMPT, user_prompt=ID_CARD_USER_PROMPT,
    )
    return extracted_data


@router.post("/documents/{document_id}/extract", response_model=ExtractionRead, status_code=201)
async def extract_from_document(
    document_id: uuid.UUID,
    session: Submission = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    document = await document_service.get_document(db, document_id)
    if not document:
        raise HTTPException(404, "Document not found")
    if document.submission_id != session.id:
        raise HTTPException(403, "Document does not belong to this session")

    if document.extraction:
        raise HTTPException(409, "Extraction already exists for this document")

    return await extraction_service.run_extraction(db, document)


@router.post("/extractions/{extraction_id}/confirm", response_model=ExtractionRead)
async def confirm_extraction(
    extraction_id: uuid.UUID,
    data: ExtractionConfirm,
    session: Submission = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    extraction = await extraction_service.get_extraction(db, extraction_id)
    if not extraction:
        raise HTTPException(404, "Extraction not found")

    document = await document_service.get_document(db, extraction.document_id)
    if not document or document.submission_id != session.id:
        raise HTTPException(403, "Extraction does not belong to this session")

    if extraction.confirmed_by_user:
        raise HTTPException(409, "Extraction already confirmed")

    return await extraction_service.confirm_extraction(db, extraction, data)
