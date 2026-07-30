"""What approval actually did, step by step.

Approving somebody enables them, and enabling them means several things landing in
several systems. Before this table those effects were fire-and-forget: the
submission recorded a few *outcomes* (a DID, a credential id, a share flag) but
nothing about which step failed, when, why, or how many times it had been tried.
A registry failure was a 422 an operator could only answer by pressing Approve
again and re-running everything.

One row per (submission, step). The row is the process record; the columns on
`submissions` remain the outcome record.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from celine.onboarding.models.database import Base


class EnablementStep(enum.StrEnum):
    """The steps, in the order they must run.

    The order is load-bearing, not stylistic. The registry keys a member on
    `(community, user_id)`, so the Keycloak user has to exist first; the dataspace
    identity is last because it is the one that can be retried afterwards.
    """

    KEYCLOAK_USER = "keycloak_user"
    REC_REGISTRY_MEMBER = "rec_registry_member"
    DATASPACE_IDENTITY = "dataspace_identity"
    DATASPACE_SHARE = "dataspace_share"


class EnablementStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # The step does not apply to this community — no dataspace binding, no
    # registry binding, no sharing consent. Recorded rather than omitted so that
    # "nothing to do" is distinguishable from "never ran".
    SKIPPED = "skipped"


class SubmissionEnablementStep(Base):
    __tablename__ = "submission_enablement_steps"
    __table_args__ = (
        UniqueConstraint("submission_id", "step", name="uq_enablement_submission_step"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("submissions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EnablementStatus.PENDING
    )

    # The id this step created in the system it talks to: a Keycloak user id, a
    # registry member key, a credential id. What an operator needs to go and look
    # at the other side.
    external_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    submission: Mapped["Submission"] = relationship(  # noqa: F821
        back_populates="enablement_steps"
    )
