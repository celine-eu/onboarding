"""add data-sharing consent fields

Block B. Records the optional data-sharing consent collected in the wizard —
which offers, at what version and locale, and the SHA-256 of the rendered text
actually shown — plus whether the standing share was provisioned to the
connector after approval.

The two booleans get a server default so the columns can be added NOT NULL to a
table that already has rows.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'submissions',
        sa.Column('data_sharing_consent', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'submissions',
        sa.Column('data_sharing_consent_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'submissions',
        sa.Column('data_sharing_consent_offer_ids', postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        'submissions',
        # Comma-joined, deduplicated versions of every accepted offer. Consent
        # is purpose-scoped, so several offers over one dataset is the intended
        # shape — sizing this for a single offer overflows at four and fails the
        # submission at its last step.
        sa.Column('data_sharing_consent_text_version', sa.String(200), nullable=True),
    )
    op.add_column(
        'submissions',
        sa.Column('data_sharing_consent_locale', sa.String(20), nullable=True),
    )
    op.add_column(
        'submissions',
        sa.Column('data_sharing_consent_text_sha256', sa.String(64), nullable=True),
    )
    op.add_column(
        'submissions',
        sa.Column('share_provisioned', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('submissions', 'share_provisioned')
    op.drop_column('submissions', 'data_sharing_consent_text_sha256')
    op.drop_column('submissions', 'data_sharing_consent_locale')
    op.drop_column('submissions', 'data_sharing_consent_text_version')
    op.drop_column('submissions', 'data_sharing_consent_offer_ids')
    op.drop_column('submissions', 'data_sharing_consent_at')
    op.drop_column('submissions', 'data_sharing_consent')
