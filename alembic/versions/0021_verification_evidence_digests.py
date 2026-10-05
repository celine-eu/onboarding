"""a verification keeps the digests of its evidence, and outlives an erasure while it backs a grant

R4 (REQ-0041..0046): the community asserts to a holder that a member holds their supply
points, citing a verification. For that to be reconstructable by audit:

- `documents.sha256`: the sha256 of the plaintext file, computed at upload.
- `submission_verifications.evidence`: the evidence digests, copied onto the row so a
  purged document leaves its digest behind.
- `submission_verifications.rec_slug` / `submission_ref`: which community and which
  application, still readable once the submission is erased. Backfilled from the
  submissions.
- `submission_verifications.last_asserted_at`: when a holder last accepted an assertion
  citing the row.
- `submission_verifications.retain_until`: set when the row was kept past its
  submission's erasure; `submission_id` becomes nullable for those rows.

Existing documents and verifications get no digest: the files are encrypted and the key
is not the migration's. A digest is computed when a verification is recorded against an
older document whose file is still stored.

Downgrade deletes the rows kept past an erasure (they have no submission) before
`submission_id` becomes NOT NULL again.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "submission_verifications"


def upgrade() -> None:
    op.add_column("documents", sa.Column("sha256", sa.String(64), nullable=True))

    op.add_column(TABLE, sa.Column("rec_slug", sa.String(100), nullable=True))
    op.add_column(TABLE, sa.Column("submission_ref", sa.String(64), nullable=True))
    op.add_column(TABLE, sa.Column("evidence", postgresql.JSONB(), nullable=True))
    op.add_column(TABLE, sa.Column("last_asserted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("retain_until", sa.DateTime(timezone=True), nullable=True))
    op.alter_column(TABLE, "submission_id", existing_type=postgresql.UUID(), nullable=True)

    op.execute(
        f"""
        UPDATE {TABLE} AS v
           SET rec_slug = s.rec_slug, submission_ref = s.ref
          FROM submissions AS s
         WHERE v.submission_id = s.id
        """
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM {TABLE} WHERE submission_id IS NULL")
    op.alter_column(TABLE, "submission_id", existing_type=postgresql.UUID(), nullable=False)
    op.drop_column(TABLE, "retain_until")
    op.drop_column(TABLE, "last_asserted_at")
    op.drop_column(TABLE, "evidence")
    op.drop_column(TABLE, "submission_ref")
    op.drop_column(TABLE, "rec_slug")

    op.drop_column("documents", "sha256")
