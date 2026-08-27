import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.models.document import Document
from celine.onboarding.models.extraction import Extraction
from celine.onboarding.models.schemas import ExtractionConfirm
from celine.onboarding.services.document_service import read_file


async def run_extraction(db: AsyncSession, document: Document) -> Extraction:
    from celine.onboarding.extractors.openai_extractor import OpenAIExtractor

    image_bytes = read_file(document)

    extractor = OpenAIExtractor()
    extracted_data, raw_response = await extractor.extract(image_bytes, document.mime_type)

    extraction = Extraction(
        document_id=document.id,
        extracted_data=extracted_data,
        raw_response=raw_response,
    )
    db.add(extraction)
    await db.commit()
    await db.refresh(extraction)
    return extraction


async def get_extraction(db: AsyncSession, extraction_id: uuid.UUID) -> Extraction | None:
    result = await db.execute(select(Extraction).where(Extraction.id == extraction_id))
    return result.scalar_one_or_none()


async def confirm_extraction(
    db: AsyncSession, extraction: Extraction, data: ExtractionConfirm
) -> Extraction:
    if data.extracted_data is not None:
        extraction.extracted_data = data.extracted_data
    extraction.confirmed_by_user = True
    extraction.confirmed_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(extraction)
    return extraction
