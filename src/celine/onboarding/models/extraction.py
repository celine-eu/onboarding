import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from celine.onboarding.models.database import Base

if TYPE_CHECKING:  # relationship annotations only — SQLAlchemy resolves
    # these strings at mapper configuration time, so importing them at
    # runtime would be a circular import for no benefit.
    from celine.onboarding.models.document import Document
from celine.onboarding.models.encrypted import EncryptedJSON


class Extraction(Base):
    __tablename__ = "extractions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), unique=True
    )
    extracted_data: Mapped[dict] = mapped_column(EncryptedJSON, default=dict)
    raw_response: Mapped[dict] = mapped_column(EncryptedJSON, default=dict)
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped["Document"] = relationship(back_populates="extraction")
