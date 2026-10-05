import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from celine.onboarding.extractors.fields import BILL_FIELDS, ID_CARD_FIELDS, keep_fields
from celine.onboarding.models.document import Document, DocumentType
from celine.onboarding.models.extraction import Extraction
from celine.onboarding.models.schemas import ExtractionConfirm
from celine.onboarding.models.submission import Submission, SubmissionStatus
from celine.onboarding.services.document_service import read_file

#: What a scan read from the participant's documents. The service writes these,
#: from the extraction provider's answer; the participant's own PATCH cannot. They
#: are what an operator compares the declared name, tax code and POD against, so
#: an applicant who could rewrite them could make any declaration look confirmed
#: by their documents. The participant corrects the declared fields instead
#: (`first_name`, `fiscal_code`, `pod_code`, `supply_address`, ...); an operator
#: may still change these through the admin API, which records who did.
SCANNED_FIELDS = frozenset({"extracted_data", "id_extracted_data"})


class ScannedValueEditError(ValueError):
    """A participant tried to change what a scan read."""


def merge_scan(previous: dict | None, new: dict, fields: tuple[str, ...]) -> dict:
    """``new`` over ``previous``, the wizard's rule: a non-empty value is kept,
    and replaced only by a longer one. Each scan re-reads every page uploaded so
    far, so a later, poorer read of the same page does not erase a good one."""
    merged = keep_fields(previous, fields)
    for key in fields:
        value = new.get(key)
        if not value:
            continue
        existing = merged.get(key)
        if not existing or len(str(value)) > len(str(existing)):
            merged[key] = value
    return merged


async def record_scan(
    db: AsyncSession, submission: Submission, *, id_card: bool, data: dict
) -> None:
    """Keep what a scan read on the participant's draft, as the service saw it."""
    if submission.status != SubmissionStatus.DRAFT:
        return
    fields = ID_CARD_FIELDS if id_card else BILL_FIELDS
    attribute = "id_extracted_data" if id_card else "extracted_data"
    setattr(submission, attribute, merge_scan(getattr(submission, attribute), data, fields))
    await db.commit()


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
        # A confirmation, not an edit: what the scan read stays what it read.
        fields = (
            ID_CARD_FIELDS if extraction.document.doc_type == DocumentType.ID_CARD else BILL_FIELDS
        )
        stored = extraction.extracted_data or {}
        changed = sorted(
            key
            for key, value in keep_fields(data.extracted_data, fields).items()
            if key in data.extracted_data and value != stored.get(key)
        )
        if changed:
            raise ScannedValueEditError(
                f"{', '.join(changed)}: what a scan read cannot be changed; "
                "correct the application's own fields instead"
            )
    extraction.confirmed_by_user = True
    extraction.confirmed_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(extraction)
    return extraction
