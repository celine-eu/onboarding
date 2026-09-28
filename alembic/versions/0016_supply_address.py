"""add supply_address

The supply address the wizard's eligibility step checked, stored so that the
service can resolve the primary-substation boundary from it at submit and at
approval (REQ-0018, D48). Until now the only supply address a submission held
was the scanned `extracted_data.indirizzo`, so a boundary community with
document scanning off could not submit at all.

A JSON object in the shape the geocoder takes (`{"text": "<address>"}`),
encrypted like every other fragment of the participant's address. The geocoded
coordinates are still not stored anywhere.

Nothing is backfilled: earlier submissions keep resolving from the scanned
address.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column("supply_address", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("submissions", "supply_address")
