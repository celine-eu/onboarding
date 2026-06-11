"""add recs table and rec_slug to submissions

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'recs',
        sa.Column('slug', sa.String(40), primary_key=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('manifest', postgresql.JSONB(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.execute(
        "INSERT INTO recs (slug, name, manifest) "
        "VALUES ('default', 'Default', '{}'::jsonb) "
        "ON CONFLICT DO NOTHING"
    )

    op.add_column(
        'submissions',
        sa.Column('rec_slug', sa.String(40), nullable=False, server_default='default'),
    )
    op.create_foreign_key(
        'fk_submissions_rec_slug', 'submissions', 'recs',
        ['rec_slug'], ['slug'],
    )
    op.create_index('ix_submissions_rec_slug', 'submissions', ['rec_slug'])
    op.create_index('ix_submissions_rec_created', 'submissions', ['rec_slug', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_submissions_rec_created', 'submissions')
    op.drop_index('ix_submissions_rec_slug', 'submissions')
    op.drop_constraint('fk_submissions_rec_slug', 'submissions', type_='foreignkey')
    op.drop_column('submissions', 'rec_slug')
    op.drop_table('recs')
