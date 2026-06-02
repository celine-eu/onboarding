import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from celine.onboarding.models.database import Base


class DocumentType(str, enum.Enum):
    UTILITY_BILL = "utility_bill"
    GDPR_FORM = "gdpr_form"
    POLICY_DOC = "policy_doc"
    STATUTE_DOC = "statute_doc"
    OTHER = "other"


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("submissions.id", ondelete="CASCADE")
    )
    doc_type: Mapped[DocumentType] = mapped_column(Enum(DocumentType))
    file_path: Mapped[str] = mapped_column(String(500))
    original_filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    submission: Mapped["Submission"] = relationship(back_populates="documents")
    extraction: Mapped["Extraction | None"] = relationship(back_populates="document")
