"""an applicant may declare they are already a member

`submissions.declared_existing_member`: the applicant ticked "I am already a
member" on a template that offers it (REQ-0024). The POD and the supply
address are then not required at submit; the operator completes both from the
community's member register, by revision, before approval (REQ-0025).

Existing rows are `false`: nobody declared anything before this column.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-02

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column(
            "declared_existing_member",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("submissions", "declared_existing_member")
