"""approval waits for the REC's recorded verification

`submission_verifications` holds how the community verified a participant's
identity and that they hold the POD: checked offline, or confirmed against a
document stored on the submission. Approval refuses without one. Rows are never
updated; a correction is a new row, and the newest is the one in force.

Nothing is backfilled. Submissions already approved were approved before this
existed and are not touched; submissions still in review need a verification
like any other.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0012'
down_revision: Union[str, None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'submission_verifications',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'submission_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('submissions.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('method', sa.String(32), nullable=False),
        sa.Column(
            'document_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('documents.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('actor_type', sa.String(20), nullable=False),
        sa.Column('actor_sub', sa.String(255), nullable=True),
        sa.Column('actor_email', sa.String(320), nullable=True),
        sa.Column('actor_client_id', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        'ix_submission_verifications_submission_id',
        'submission_verifications',
        ['submission_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_submission_verifications_submission_id', 'submission_verifications')
    op.drop_table('submission_verifications')
