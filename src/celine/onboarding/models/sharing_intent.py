"""The member's latest decision on one offer, as they pressed it on their page.

A connector records a decision only where it lands, and not always even then: ds
stamps nothing when a withdrawal meets one that already stands, and a relayed
grant written over a withdrawal replaces the row the next read would have shown.
So a member's newest act can be recorded nowhere a later reader can see — and the
operator's retry, which brings every connector to the member's newest decision,
would re-drive an older one. This row is that act, kept where the act happened.

One row per ``(subject_id, offer_id)``: the newest decision, never a history. The
connectors and provenance keep the history; the retry needs only what is newest.

Written by the member's toggle only. The form's acceptance is already on the
submission, and nothing else here decides on the member's behalf.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from celine.onboarding.models.database import Base


class MemberSharingIntent(Base):
    __tablename__ = "member_sharing_intents"
    __table_args__ = (
        UniqueConstraint("subject_id", "offer_id", name="uq_member_sharing_intent_subject_offer"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The member's dataspace DID — what ds keys a decision on, and what the retry
    #: knows them by (`submissions.dataspace_did`). Not an email and not a name.
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    offer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The community the member decided in; for a reader, not for the key.
    rec_slug: Mapped[str] = mapped_column(String(40), nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: When the member pressed, by this service's clock. Ranked against the
    #: connectors' own times, which assumes the clocks agree (plan, "Clock skew").
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: What this service served for the offer when they decided: codes and a hash,
    #: never anything about the person. Relayed with a grant; kept with a withdrawal
    #: to say which version was withdrawn.
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
