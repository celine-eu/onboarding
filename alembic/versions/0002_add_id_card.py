"""add ID_CARD document type and id_extracted_data column

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-06 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE documenttype ADD VALUE IF NOT EXISTS 'ID_CARD'")
    op.add_column('submissions', sa.Column('id_extracted_data', postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('submissions', 'id_extracted_data')
