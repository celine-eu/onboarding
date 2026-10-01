"""a revision after approval propagates, step by step

`submission_revision_steps` records, for each revision recorded after approval,
what carrying the new value to each other system did: the Keycloak account,
the REC registry member, the identity registry's mapping. One row per
(revision, step), with its state (`pending`, `done`, `failed`, `skipped`), the
attempts, the last error's code and a sentence for the operator. No value is
stored here: the values are in `submission_revisions`, encrypted.

Nothing is backfilled: revisions recorded before this table propagated nothing,
and say so on the console.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "submission_revision_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("submission_revisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step", sa.String(40), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outcome", sa.String(40), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("revision_id", "step", name="uq_revision_step"),
    )
    op.create_index(
        "ix_submission_revision_steps_revision_id",
        "submission_revision_steps",
        ["revision_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_submission_revision_steps_revision_id", "submission_revision_steps")
    op.drop_table("submission_revision_steps")
