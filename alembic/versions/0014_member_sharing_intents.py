"""record the member's latest decision per sharing offer

`member_sharing_intents` holds, per member (dataspace DID) and offer, the newest
decision the member took on their sharing page: granted or withdrawn, when, and
what this service served for the offer at the time. The member's toggle writes it
before relaying the decision to any connector; the operator's retry ranks it with
what the connectors record, so a withdrawal a connector did not stamp, or one a
relayed grant overwrote, is still the member's newest decision.

Nothing is backfilled: no earlier toggle was recorded here, and inventing one
would be a claim about what a person decided. Until a member presses again, the
retry ranks the connectors' rows alone, as it did before.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0014'
down_revision: Union[str, None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'member_sharing_intents',
        sa.Column(
            'id',
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text('gen_random_uuid()'),
        ),
        sa.Column('subject_id', sa.String(255), nullable=False),
        sa.Column('offer_id', sa.String(255), nullable=False),
        sa.Column('rec_slug', sa.String(40), nullable=False),
        sa.Column('granted', sa.Boolean(), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('evidence', postgresql.JSONB(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            'subject_id', 'offer_id', name='uq_member_sharing_intent_subject_offer'
        ),
    )


def downgrade() -> None:
    op.drop_table('member_sharing_intents')
