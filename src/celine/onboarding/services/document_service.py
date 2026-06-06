import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from celine.onboarding.config.settings import settings
from celine.onboarding.models.document import Document, DocumentType

ALLOWED_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
}


async def save_document(
    db: AsyncSession,
    submission_id: uuid.UUID,
    file: UploadFile,
    doc_type: DocumentType,
) -> Document:
    max_size = settings.max_upload_size_mb * 1024 * 1024
    if file.size and file.size > max_size:
        raise ValueError(f"File too large (max {settings.max_upload_size_mb}MB)")

    content = await file.read()
    size = len(content)
    if size > max_size:
        raise ValueError(f"File too large: {size} bytes (max {max_size})")

    from celine.onboarding.extractors.openai_extractor import _detect_mime
    detected = _detect_mime(content)
    mime = detected if detected != "application/octet-stream" else (file.content_type or "")
    if mime not in ALLOWED_MIME_TYPES:
        raise ValueError(f"Unsupported file type: {mime}")

    doc_id = uuid.uuid4()
    raw_name = Path(file.filename or "file").name
    ext = Path(raw_name).suffix or ".bin"
    from celine.onboarding.services.submission_service import get_submission

    submission = await get_submission(db, submission_id)
    folder_name = submission.ref if submission else str(submission_id)
    relative_path = f"submissions/{folder_name}/{doc_id}{ext}"
    from celine.onboarding.services.crypto import encrypt

    full_path = Path(settings.data_dir) / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(encrypt(content))

    document = Document(
        id=doc_id,
        submission_id=submission_id,
        doc_type=doc_type,
        file_path=relative_path,
        original_filename=file.filename or "unknown",
        mime_type=mime,
        size_bytes=size,
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)
    return document


async def get_document(db: AsyncSession, document_id: uuid.UUID) -> Document | None:
    result = await db.execute(
        select(Document)
        .where(Document.id == document_id)
        .options(selectinload(Document.extraction))
    )
    return result.scalar_one_or_none()


async def list_documents(db: AsyncSession, submission_id: uuid.UUID) -> list[Document]:
    result = await db.execute(
        select(Document)
        .where(Document.submission_id == submission_id)
        .order_by(Document.created_at)
    )
    return list(result.scalars().all())


def get_file_path(document: Document) -> Path:
    return Path(settings.data_dir) / document.file_path


def read_file(document: Document) -> bytes:
    from celine.onboarding.services.crypto import decrypt

    path = get_file_path(document)
    return decrypt(path.read_bytes())
