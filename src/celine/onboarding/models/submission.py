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
    # Municipality of the supply address, as resolved by the eligibility
    # geocoder. Kept because it decides which registry area the member is
    # registered into, and the geocoder resolves it far more reliably than OCR
    # of a bill does. Encrypted like the rest of the address data it comes from.
    supply_municipality: Mapped[str | None] = mapped_column(
        EncryptedString, nullable=True
    )

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

    # Data-sharing consent — deliberately optional. Conditioning REC membership
    # on dataspace sharing would breach GDPR Art. 7(4), so this is never a
    # submit requirement (see workflows/engine.can_submit). The offers, version,
    # locale and rendered-text hash record *what* the person saw, so the
    # connector can enforce exactly that and re-consent only on a material change.
    data_sharing_consent: Mapped[bool] = mapped_column(default=False)
    data_sharing_consent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    data_sharing_consent_offer_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Comma-joined, deduplicated versions of every accepted offer. Sized for the
    # multi-offer case consent scoping is built for, not just today's single one.
    data_sharing_consent_text_version: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    data_sharing_consent_locale: Mapped[str | None] = mapped_column(String(20), nullable=True)
    data_sharing_consent_text_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    # Whether the standing share was pushed to the connector after approval. A
    # failed push never fails approval (§3.5) — it is retried from the admin UI.
    share_provisioned: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    documents: Mapped[list["Document"]] = relationship(back_populates="submission")

    # What approval did, step by step. Cascade-deleted with the submission so a
    # GDPR erasure leaves no trace of the person's provisioning either.
    enablement_steps: Mapped[list["SubmissionEnablementStep"]] = relationship(  # noqa: F821
        back_populates="submission",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
