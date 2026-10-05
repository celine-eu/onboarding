"""The REC's verification of who a participant is and that they hold the POD.

Onboarding does not require documents and does not want every member's identity
document stored here. What makes an approval credible is that the community
checked, on its own responsibility, and said how. So approval waits for a
verification, not for an upload.

Rows are never edited or deleted: a correction is a new row that supersedes the
previous one, and the newest row is the one in force. The history is what lets
anyone reading it later see that a verification was corrected, and by whom.

**A verification can back a grant at another participant** (R4, REQ-0041..0046).
When the community registers a member's consent at a holder with the member's
supply points, it asserts — on its own responsibility — that the member holds
them, citing this row (`services/key_assertion.py`). Such a row carries the
digests of the evidence it rested on, and is kept, detached from an erased
submission, for the retention period (`retain_until`).
"""

import enum
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
    #: The REC checked the person's documents outside the platform, and the
    #: platform holds nothing of them. Not accepted where a grant would carry the
    #: member's supply points to another participant (REQ-0042).
    OFFLINE = "offline"
    #: The operator confirmed a document stored on this submission.
    UPLOADED_DOCUMENT = "uploaded-document"
    #: The REC checked the person's documents outside the platform and the operator
    #: gave the platform the evidence file it keeps, which was hashed and not
    #: stored: only its digest is recorded (REQ-0042).
    OFFLINE_WITH_EVIDENCE = "offline-with-evidence"

    @property
    def carries_evidence(self) -> bool:
        """Whether a row of this method holds at least one evidence digest."""
        return self is not VerificationMethod.OFFLINE

    @property
    def credential_value(self) -> str:
        """What the dataspace credential and the registry member carry."""
        return f"{CREDENTIAL_METHOD_PREFIX}:{self.value}"


class SubmissionVerification(Base):
    __tablename__ = "submission_verifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # NULL once the submission was erased and this row was kept because it backs
    # an assertion sent to a holder (`retain_until`). Otherwise the row goes with
    # the submission.
    submission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("submissions.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    # Copied at record time so a row kept after its submission was erased still
    # says which community and which application it was. The ref is the one
    # identifier that already leaves onboarding (`submission_ref`).
    rec_slug: Mapped[str | None] = mapped_column(String(100), nullable=True)
    submission_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # A string rather than a database enum, like `audit_logs.actor_type`: the set
    # may grow, and `VerificationMethod` is the check.
    method: Mapped[str] = mapped_column(String(32))
    # Only for `uploaded-document`. SET NULL rather than CASCADE: purging a document
    # must not erase the fact that it was checked.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    # Free text an operator writes about a person, so encrypted like the rest.
    # Cleared when the row is kept past an erasure: it is not evidence.
    note: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    # The digests of the evidence the verification rested on, as
    # `[{"kind": "utility_bill" | "id_document" | "other", "sha256": <hex>}]`.
    # Copied here (from `documents.sha256`, or computed from the file an operator
    # attached and which was not stored) so a purged document leaves its digest
    # behind. Empty for `offline`. A digest of a bill or an identity document is
    # pseudonymised personal data (ds D-1): it says nothing without the file.
    evidence: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # When an assertion citing this row was last accepted by a holder. Set means
    # the row backs a grant, and an erasure keeps it (REQ-0045).
    last_asserted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when the row was kept past its submission's erasure: until when. A row
    # is never removed before this time.
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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

    submission: Mapped["Submission | None"] = relationship(back_populates="verifications")

    @property
    def verification_method(self) -> VerificationMethod:
        return VerificationMethod(self.method)
