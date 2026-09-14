"""The REC's verification of who a participant is and that they hold the POD.

Onboarding does not require documents and does not want every member's identity
document stored here. What makes an approval credible is that the community
checked, on its own responsibility, and said how. So approval waits for a
verification, not for an upload.

Rows are never edited or deleted: a correction is a new row that supersedes the
previous one, and the newest row is the one in force. The history is what lets
anyone reading it later see that a verification was corrected, and by whom.
"""

import enum
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from celine.onboarding.models.database import Base
from celine.onboarding.models.encrypted import EncryptedString

if TYPE_CHECKING:
    from celine.onboarding.models.submission import Submission

#: Prefix of the value sent as the credential's `verificationMethod`. It was the
#: whole value before verifications were recorded, so a reader matching on it
#: still recognises every credential this service issues.
CREDENTIAL_METHOD_PREFIX = "submission-review"


class VerificationMethod(str, enum.Enum):
    #: The REC checked the person's documents outside the platform.
    OFFLINE = "offline"
    #: The operator confirmed a document stored on this submission.
    UPLOADED_DOCUMENT = "uploaded-document"

    @property
    def credential_value(self) -> str:
        """What the dataspace credential and the registry member carry."""
        return f"{CREDENTIAL_METHOD_PREFIX}:{self.value}"


class SubmissionVerification(Base):
    __tablename__ = "submission_verifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    # A string rather than a database enum, like `audit_logs.actor_type`: the set
    # may grow, and `VerificationMethod` is the check.
    method: Mapped[str] = mapped_column(String(32))
    # Only for `uploaded-document`. SET NULL rather than CASCADE: purging a document
    # must not erase the fact that it was checked.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    # Free text an operator writes about a person, so encrypted like the rest.
    note: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)

    # Who recorded it — the same four fields as the audit trail's actor.
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    actor_client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Set in Python, not by the database: two rows written in one transaction
    # would otherwise share `now()` and the newest could not be told apart.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    submission: Mapped["Submission"] = relationship(back_populates="verifications")

    @property
    def verification_method(self) -> VerificationMethod:
        return VerificationMethod(self.method)
