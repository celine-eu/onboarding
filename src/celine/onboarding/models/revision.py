"""Corrections of what a participant declared, one row per corrected field.

From `submitted` on, a POD, first name, last name or email is changed only through
a revision: the operator says how they checked the new value (by hand, or against
a document on the submission) and why, and the row keeps the value it replaced.
The `Submission` column is updated in the same transaction, so everything that
reads the column — approval, exports, the PDF — reads the corrected value.

Rows are never edited or deleted, like `submission_verifications`: the newest
revision of a field is the one in force, and the first one's `previous_value` is
what the person declared.
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from celine.onboarding.models.database import Base
from celine.onboarding.models.encrypted import EncryptedString

if TYPE_CHECKING:
    from celine.onboarding.models.submission import Submission


class SubmissionRevision(Base):
    __tablename__ = "submission_revisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    # The `Submission` column corrected. Strings rather than database enums, like
    # `method` below: `services.revision.RevisableField` is the check.
    field: Mapped[str] = mapped_column(String(32))
    # Personal data, so encrypted like the columns they come from. `previous_value`
    # is None only when the field was empty before.
    previous_value: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    new_value: Mapped[str] = mapped_column(EncryptedString)

    # How the new value was checked: `offline` or `uploaded-document` for an
    # operator (the vocabulary of `VerificationMethod`), `member-session` for the
    # member's own correction, later.
    method: Mapped[str] = mapped_column(String(32))
    # Only for `uploaded-document`. SET NULL rather than CASCADE: purging a document
    # must not erase the fact that it was checked.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    # Required of an operator. Free text about a person, so encrypted.
    note: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)

    # Who: `operator` or, later, `member` — the role the revision was made in,
    # not the token's shape (the audit row keeps that). Then the same three
    # identifying fields as the audit trail's actor.
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    actor_client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Set in Python, as for verifications: two rows written in one transaction
    # would otherwise share `now()` and the newest could not be told apart.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    submission: Mapped["Submission"] = relationship(back_populates="revisions")

    # What carrying the new value to the other systems did, one row per target.
    # Only for a revision recorded after approval: before it, nothing else holds
    # a copy. Loaded with the revision for the same reason the revisions are.
    steps: Mapped[list["SubmissionRevisionStep"]] = relationship(
        back_populates="revision",
        order_by="SubmissionRevisionStep.position",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SubmissionRevisionStep(Base):
    """One propagation target of one revision, and how far it got.

    The process record, like `submission_enablement_steps`: state, attempts, the
    last error's code and a sentence an operator can act on. **Never a value**:
    not the corrected name, address or POD, not a username — those are in the
    revision row, encrypted. A step always sends what the submission holds *now*,
    so a retry after a later correction sends the later value.
    """

    __tablename__ = "submission_revision_steps"
    __table_args__ = (UniqueConstraint("revision_id", "step", name="uq_revision_step"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("submission_revisions.id", ondelete="CASCADE"),
        index=True,
    )
    # `services.revision.PropagationStep`.
    step: Mapped[str] = mapped_column(String(40))
    # The order the steps run in, from `revision.PROPAGATION`.
    position: Mapped[int] = mapped_column(Integer, default=0)
    # pending | done | failed | skipped (`services.propagation.StepStatus`).
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # What the step answered when it did not fail: the provisioning service's
    # `verification` or `invitation` code. A code, never a value.
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # The last failure's machine-readable code (`email_taken`,
    # `account_disabled`, `http_502`, ...), cleared on success.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # A sentence for the operator: why it was skipped, what failed, what it did.
    # Written from fixed text and codes only.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    revision: Mapped[SubmissionRevision] = relationship(back_populates="steps")
