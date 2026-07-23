import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from celine.onboarding.models.database import Base
from celine.onboarding.models.encrypted import EncryptedString


class PhoneOtp(Base):
    """One issued OTP challenge.

    Rows are retained after use: the send-rate limit and the attempt lockout are
    both derived from recent history for a phone, so deleting on success would
    reset the counters an attacker is being throttled by.
    """

    __tablename__ = "phone_otps"
    __table_args__ = (
        Index("ix_phone_otps_hash_created", "phone_hash", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )

    # Encrypted for display/audit; unusable as a lookup key because Fernet is
    # non-deterministic. phone_hash is the deterministic counterpart used to
    # count sends and enforce lockout without storing the number in the clear.
    phone: Mapped[str] = mapped_column(EncryptedString)
    phone_hash: Mapped[str] = mapped_column(String(64), index=True)

    code_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
