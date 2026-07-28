"""add supply_municipality

The municipality of the supply address, as resolved by the eligibility
geocoder rather than by OCR of a utility bill. It decides which REC registry
area an approved participant is registered into, and the geocoder is markedly
more reliable than extraction — a bill states a full address as free text,
while the geocoder returns the municipality as its own field.

Encrypted, like every other fragment of the participant's address.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'submissions',
        sa.Column('supply_municipality', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('submissions', 'supply_municipality')
