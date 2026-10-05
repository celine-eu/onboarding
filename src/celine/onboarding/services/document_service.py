import hashlib
import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from celine.onboarding.config.settings import settings
from celine.onboarding.models.document import Document, DocumentType
from celine.onboarding.models.submission import Submission
from celine.onboarding.services import audit_service

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
    rec_slug = submission.rec_slug if submission else "default"
    relative_path = f"{rec_slug}/submissions/{folder_name}/{doc_id}{ext}"
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
        # Of the bytes as uploaded, before encryption: what a verification copies
        # as its evidence digest, and what the file the community keeps hashes to
        # (REQ-0041).
        sha256=hashlib.sha256(content).hexdigest(),
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


def digest(document: Document) -> str | None:
    """The document's sha256, computed from the stored file when it predates the column.

    Stored on the row once computed. ``None`` when the file is gone.
    """
    if document.sha256:
        return document.sha256
    try:
        content = read_file(document)
    except FileNotFoundError:
        return None
    document.sha256 = hashlib.sha256(content).hexdigest()
    return document.sha256


def read_file(document: Document) -> bytes:
    from celine.onboarding.services.crypto import decrypt

    path = get_file_path(document)
    return decrypt(path.read_bytes())


async def discard_documents(db: AsyncSession, submission: Submission) -> int:
    """Delete the copies a participant uploaded, once their account is active.

    A bill or an identity document is kept only for the operator to check the
    application against. Once approval has gone through, what was checked lives on
    as the verification row and in the dataspace credential's `verificationMethod`
    (`submission-review:uploaded-document`); the copy itself has no further use. A
    copy the operator downloaded before then is theirs to look after.

    Files go first, rows after: an interruption leaves a row without its file,
    which the console reports as gone (410), never a file nothing points at. The
    extractions go with their rows (`ON DELETE CASCADE`); verifications and
    revisions keep their row with `document_id` set to null, and a verification
    keeps the document's digest (`evidence`, REQ-0041). The community keeps the
    evidence itself, outside the platform, for the retention period.

    Returns how many documents were discarded.
    """
    result = await db.execute(
        select(Document.file_path).where(Document.submission_id == submission.id)
    )
    paths = list(result.scalars().all())
    if not paths:
        return 0

    for relative in paths:
        (Path(settings.data_dir) / relative).unlink(missing_ok=True)
    folder = Path(settings.data_dir) / submission.rec_slug / "submissions" / submission.ref
    if folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()

    await db.execute(delete(Document).where(Document.submission_id == submission.id))
    audit_service.record(
        db,
        action="discard_documents",
        entity_type="submission",
        entity_id=str(submission.id),
        actor=audit_service.Actor.system("account activated"),
        rec_slug=submission.rec_slug,
        detail=f"ref={submission.ref} documents={len(paths)}",
    )
    await db.commit()
    return len(paths)
