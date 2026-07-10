"""add dataspace identity fields

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('submissions', sa.Column('dataspace_subject_id', sa.String(128), nullable=True))
    op.add_column('submissions', sa.Column('dataspace_did', sa.String(255), nullable=True))
    op.add_column('submissions', sa.Column('dataspace_vc_id', sa.String(255), nullable=True))
    op.add_column(
        'submissions',
        sa.Column('dataspace_vc_issued_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_submissions_dataspace_subject_id', 'submissions', ['dataspace_subject_id'])


def downgrade() -> None:
    op.drop_index('ix_submissions_dataspace_subject_id', 'submissions')
    op.drop_column('submissions', 'dataspace_vc_issued_at')
    op.drop_column('submissions', 'dataspace_vc_id')
    op.drop_column('submissions', 'dataspace_did')
    op.drop_column('submissions', 'dataspace_subject_id')
