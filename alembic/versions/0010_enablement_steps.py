"""record what approval did, step by step

Approval lands several things in several systems — a login, a community member, a
dataspace identity, a standing sharing consent. Until now the only record was a
handful of outcome columns on `submissions`, so "which step failed and why" was a
log line at best, and the only remedy was to press Approve again and re-run
everything.

Backfill is deliberately conservative. Two steps have positive evidence on the
submission — `dataspace_vc_id` for the credential, `share_provisioned` for the
consent — and those become `succeeded`. The other two have none: nothing ever
persisted the Keycloak user id or the registry member key, and inferring
`succeeded` from `status = APPROVED` would be recording a guess as a fact. They
stay `pending`, which is retriable and idempotent, so the worst case is an
operator re-runs a step that had already run.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0010'
down_revision: Union[str, None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'submission_enablement_steps',
        sa.Column(
            'id',
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text('gen_random_uuid()'),
        ),
        sa.Column(
            'submission_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('submissions.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('step', sa.String(40), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('external_ref', sa.String(255), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint('submission_id', 'step', name='uq_enablement_submission_step'),
    )
    op.create_index(
        'ix_submission_enablement_steps_submission_id',
        'submission_enablement_steps',
        ['submission_id'],
    )

    # Every approved submission gets a full set of rows, so the console shows a
    # complete pipeline rather than a partial one.
    op.execute(
        """
        INSERT INTO submission_enablement_steps (submission_id, step, status, detail)
        SELECT s.id,
               step.name,
               CASE
                   WHEN step.name = 'dataspace_identity' AND s.dataspace_vc_id IS NOT NULL
                       THEN 'succeeded'
                   WHEN step.name = 'dataspace_share' AND s.share_provisioned
                       THEN 'succeeded'
                   ELSE 'pending'
               END,
               'backfilled at migration 0010'
          FROM submissions AS s
         CROSS JOIN (
             VALUES ('keycloak_user'), ('rec_registry_member'),
                    ('dataspace_identity'), ('dataspace_share')
         ) AS step(name)
         WHERE s.status = 'APPROVED'
        """
    )

    op.execute(
        """
        UPDATE submission_enablement_steps AS e
           SET external_ref = s.dataspace_vc_id
          FROM submissions AS s
         WHERE e.submission_id = s.id
           AND e.step = 'dataspace_identity'
           AND e.status = 'succeeded'
        """
    )


def downgrade() -> None:
    op.drop_index(
        'ix_submission_enablement_steps_submission_id',
        table_name='submission_enablement_steps',
    )
    op.drop_table('submission_enablement_steps')
