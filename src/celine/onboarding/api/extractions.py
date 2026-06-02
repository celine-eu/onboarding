import uuid

from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.api.deps import limiter
from celine.onboarding.models.database import get_db
from celine.onboarding.models.schemas import ExtractionConfirm, ExtractionRead
from celine.onboarding.services import document_service, extraction_service

router = APIRouter(tags=["extractions"])

ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}


@router.post("/extract")
@limiter.limit("10/hour")
async def extract_from_upload(request: Request, files: Annotated[list[UploadFile], File()]):
    """Extract structured data from bill pages (images/PDFs). Stateless."""
    from celine.onboarding.extractors.openai_extractor import OpenAIExtractor

    pages = []
    for f in files:
        if f.content_type not in ALLOWED_MIME_TYPES:
            raise HTTPException(400, f"Unsupported file type: {f.content_type}")
        pages.append((await f.read(), f.content_type or "application/octet-stream"))

    extractor = OpenAIExtractor()
    extracted_data, _ = await extractor.extract_pages(pages)
    return extracted_data


@router.post("/documents/{document_id}/extract", response_model=ExtractionRead, status_code=201)
async def extract_from_document(
    document_id: uuid.UUID, db: AsyncSession = Depends(get_db)
):
    document = await document_service.get_document(db, document_id)
    if not document:
        raise HTTPException(404, "Document not found")

    if document.extraction:
        raise HTTPException(409, "Extraction already exists for this document")

    return await extraction_service.run_extraction(db, document)


@router.post("/extractions/{extraction_id}/confirm", response_model=ExtractionRead)
async def confirm_extraction(
    extraction_id: uuid.UUID,
    data: ExtractionConfirm,
    db: AsyncSession = Depends(get_db),
):
    extraction = await extraction_service.get_extraction(db, extraction_id)
    if not extraction:
        raise HTTPException(404, "Extraction not found")

    if extraction.confirmed_by_user:
        raise HTTPException(409, "Extraction already confirmed")

    return await extraction_service.confirm_extraction(db, extraction, data)
