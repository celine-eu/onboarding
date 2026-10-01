"""an operator corrects declared data by revision

`submission_revisions` holds every correction of a submission's POD, first name,
last name or email from `submitted` on: the value replaced and the new one
(encrypted), how the new value was checked (`offline` or `uploaded-document`),
the operator's note (encrypted) and who made it. Rows are never updated; the
newest revision of a field is the one in force, and the first one's previous
value is what the person declared.

Nothing is backfilled: no revision existed before this table did.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "submission_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "submission_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("submissions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field", sa.String(32), nullable=False),
        sa.Column("previous_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_sub", sa.String(255), nullable=True),
        sa.Column("actor_email", sa.String(320), nullable=True),
        sa.Column("actor_client_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_submission_revisions_submission_id",
        "submission_revisions",
        ["submission_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_submission_revisions_submission_id", "submission_revisions")
    op.drop_table("submission_revisions")
