"""add phone verification (OTP)

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'submissions',
        sa.Column('phone_verified', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'submissions',
        sa.Column('phone_verified_at', sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        'phone_otps',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'submission_id',
            UUID(as_uuid=True),
            sa.ForeignKey('submissions.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('phone', sa.Text(), nullable=False),
        sa.Column('phone_hash', sa.String(64), nullable=False),
        sa.Column('code_hash', sa.String(64), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_phone_otps_submission_id', 'phone_otps', ['submission_id'])
    op.create_index('ix_phone_otps_phone_hash', 'phone_otps', ['phone_hash'])
    op.create_index('ix_phone_otps_hash_created', 'phone_otps', ['phone_hash', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_phone_otps_hash_created', 'phone_otps')
    op.drop_index('ix_phone_otps_phone_hash', 'phone_otps')
    op.drop_index('ix_phone_otps_submission_id', 'phone_otps')
    op.drop_table('phone_otps')
    op.drop_column('submissions', 'phone_verified_at')
    op.drop_column('submissions', 'phone_verified')
