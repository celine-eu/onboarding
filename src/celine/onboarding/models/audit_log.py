import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from celine.onboarding.models.database import Base

# Who took the action. Not a database enum: the set grows as the console does,
# and a migration per value is not worth the constraint.
#
#   user    an operator's Keycloak identity (actor_sub, actor_email set)
#   service a client_credentials token (actor_client_id set)
#   cli     onboarding-cli in --local break-glass mode (actor_sub = os-user@host)
#   system  the platform itself, e.g. a scheduled retry with no caller
#   token   the shared-ADMIN_TOKEN era, which had no actor at all — the
#           accountability gap this column exists to close. Recorded explicitly
#           rather than left NULL so that "we cannot know who did this" is a fact
#           in the trail instead of missing data.
ACTOR_TYPES = ("user", "service", "cli", "system", "token")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        # The console reads one community's trail at a time, newest first.
        Index("ix_audit_logs_rec_slug_created_at", "rec_slug", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    action: Mapped[str] = mapped_column(String(50))
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Which community the action concerned. Nullable because rows written before
    # the console was multi-tenant cannot all be attributed — the backfill
    # recovers the ones whose entity is a submission, and the rest stay unknown
    # rather than being guessed into somebody's trail.
    rec_slug: Mapped[str | None] = mapped_column(String(40), nullable=True)

    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="token"
    )
    # Keycloak `sub` for a user, or `os-user@host` for the local CLI.
    actor_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Kept alongside `actor_sub` on purpose: a `sub` is opaque, and an operator
    # reading a trail needs to recognise a colleague without a Keycloak lookup.
    actor_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    actor_client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
