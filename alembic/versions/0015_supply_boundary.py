"""add supply_boundary_id and supply_boundary_source

The primary-substation boundary the submission's supply address falls in, for a
community whose template declares its areas as boundaries (ADR-0012, ADR-0013).
Resolved by the service from the submission's own supply address on save, on
submit and again at approval; never sent by a client. The coordinates the
geocoder returned are not stored anywhere: the id is all later steps need.

`supply_boundary_id` is encrypted like every other fragment of the participant's
address. `supply_boundary_source` names a public reference table and is not.

Nothing is backfilled: no earlier submission was resolved against a boundary.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-27

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column("supply_boundary_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "submissions",
        sa.Column("supply_boundary_source", sa.String(40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("submissions", "supply_boundary_source")
    op.drop_column("submissions", "supply_boundary_id")
