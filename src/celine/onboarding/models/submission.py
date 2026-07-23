import enum
import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from celine.onboarding.models.database import Base
from celine.onboarding.models.encrypted import EncryptedJSON, EncryptedString


def _sortable_ref() -> str:
    date_prefix = datetime.now(timezone.utc).strftime("%Y%m%d")
    short_id = uuid.uuid4().hex[:8]
    return f"{date_prefix}-{short_id}"


class SubmissionStatus(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        Index("ix_submissions_rec_created", "rec_slug", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ref: Mapped[str] = mapped_column(String(20), unique=True, default=_sortable_ref)
    rec_slug: Mapped[str] = mapped_column(
        String(40), ForeignKey("recs.slug"), nullable=False, index=True,
        server_default="default",
    )
    status: Mapped[SubmissionStatus] = mapped_column(
        Enum(SubmissionStatus), default=SubmissionStatus.DRAFT
    )

    first_name: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    last_name: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    email: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    phone: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    fiscal_code: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    pod_code: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)

    # Session binding — token ties the session to the browser tab
    session_token: Mapped[str] = mapped_column(
        String(64), default=lambda: secrets.token_urlsafe(32)
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    extracted_data: Mapped[dict | None] = mapped_column(EncryptedJSON, nullable=True)
    id_extracted_data: Mapped[dict | None] = mapped_column(EncryptedJSON, nullable=True)

    # Dynamic fields from manifest (PV, battery, community-specific questions)
    extra_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Consents (collected first, with audit trail)
    consent_ip: Mapped[str] = mapped_column(EncryptedString)
    gdpr_consent: Mapped[bool] = mapped_column(default=False)
    gdpr_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gdpr_consent_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    policy_consent: Mapped[bool] = mapped_column(default=False)
    policy_consent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    policy_consent_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    statute_consent: Mapped[bool] = mapped_column(default=False)
    statute_consent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    statute_consent_version: Mapped[str | None] = mapped_column(String(20), nullable=True)

    keep_me_updated: Mapped[bool] = mapped_column(default=False)

    phone_verified: Mapped[bool] = mapped_column(default=False, nullable=False)
    phone_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    dataspace_subject_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dataspace_did: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dataspace_vc_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dataspace_vc_issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    documents: Mapped[list["Document"]] = relationship(back_populates="submission")
