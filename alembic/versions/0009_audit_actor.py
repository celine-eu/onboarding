"""audit trail gains an actor and a community

Before this, every admin action was attributed to an IP address. Authorization
was a single shared `ADMIN_TOKEN`, so there was nothing else to record: the trail
could say a submission was approved but not by whom, and `GET /audit-logs`
returned the whole deployment's history to any token holder because rows carried
no community either.

`actor_type` defaults to `token` rather than being nullable. Existing rows *were*
taken by whoever held the shared token, and saying so is more useful than a NULL
that reads like missing data.

`rec_slug` is backfilled for rows whose entity is a submission, by joining on the
submission id. Rows that name no entity — the `list` actions — cannot be
attributed to a community and stay NULL: their `detail` does carry `rec=<slug>`,
but parsing a log message into a scoping column is how a trail acquires wrong
facts. They fall out of the per-community view, which is the correct reading of
"we do not know which community this concerned".

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0009'
down_revision: Union[str, None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('audit_logs', sa.Column('rec_slug', sa.String(40), nullable=True))
    op.add_column(
        'audit_logs',
        sa.Column(
            'actor_type',
            sa.String(20),
            nullable=False,
            server_default='token',
        ),
    )
    op.add_column('audit_logs', sa.Column('actor_sub', sa.String(255), nullable=True))
    op.add_column('audit_logs', sa.Column('actor_email', sa.String(320), nullable=True))
    op.add_column(
        'audit_logs', sa.Column('actor_client_id', sa.String(255), nullable=True)
    )

    # Recover the community for submission-scoped rows. `entity_id` is a free-form
    # String(100), so the cast is guarded by a UUID shape test rather than
    # attempted blindly — one malformed id must not fail the migration.
    op.execute(
        """
        UPDATE audit_logs AS a
           SET rec_slug = s.rec_slug
          FROM submissions AS s
         WHERE a.entity_type = 'submission'
           AND a.entity_id IS NOT NULL
           AND a.entity_id ~
               '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
           AND a.entity_id::uuid = s.id
        """
    )

    op.create_index(
        'ix_audit_logs_rec_slug_created_at',
        'audit_logs',
        ['rec_slug', 'created_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_audit_logs_rec_slug_created_at', table_name='audit_logs')
    op.drop_column('audit_logs', 'actor_client_id')
    op.drop_column('audit_logs', 'actor_email')
    op.drop_column('audit_logs', 'actor_sub')
    op.drop_column('audit_logs', 'actor_type')
    op.drop_column('audit_logs', 'rec_slug')
