import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from celine.onboarding.extractors.fields import BILL_FIELDS, ID_CARD_FIELDS, keep_fields
from celine.onboarding.models.document import Document, DocumentType
from celine.onboarding.models.extraction import Extraction
from celine.onboarding.models.schemas import ExtractionConfirm
from celine.onboarding.services.document_service import read_file


async def run_extraction(db: AsyncSession, document: Document) -> Extraction:
    from celine.onboarding.extractors.openai_extractor import OpenAIExtractor

    image_bytes = read_file(document)

    extractor = OpenAIExtractor()
    if document.doc_type == DocumentType.ID_CARD:
        from celine.onboarding.extractors.openai_extractor import (
            ID_CARD_SYSTEM_PROMPT,
            ID_CARD_USER_PROMPT,
        )

        extracted_data, raw_response = await extractor.extract(
            image_bytes,
            document.mime_type,
            system_prompt=ID_CARD_SYSTEM_PROMPT,
            user_prompt=ID_CARD_USER_PROMPT,
            fields=ID_CARD_FIELDS,
        )
    else:
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
    result = await db.execute(
        select(Extraction)
        .where(Extraction.id == extraction_id)
        .options(selectinload(Extraction.document))
    )
    return result.scalar_one_or_none()


async def confirm_extraction(
    db: AsyncSession, extraction: Extraction, data: ExtractionConfirm
) -> Extraction:
    if data.extracted_data is not None:
        fields = (
            ID_CARD_FIELDS if extraction.document.doc_type == DocumentType.ID_CARD else BILL_FIELDS
        )
        extraction.extracted_data = keep_fields(data.extracted_data, fields)
    extraction.confirmed_by_user = True
    extraction.confirmed_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(extraction)
    return extraction
