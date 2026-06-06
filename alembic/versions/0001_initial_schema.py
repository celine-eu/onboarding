"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-06-06 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'submissions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('ref', sa.String(20), nullable=False),
        sa.Column('status', sa.Enum('DRAFT', 'SUBMITTED', 'UNDER_REVIEW', 'APPROVED', 'REJECTED', name='submissionstatus'), nullable=False),
        sa.Column('first_name', sa.String(100), nullable=True),
        sa.Column('last_name', sa.String(100), nullable=True),
        sa.Column('email', sa.Text(), nullable=True),
        sa.Column('phone', sa.Text(), nullable=True),
        sa.Column('fiscal_code', sa.Text(), nullable=True),
        sa.Column('pod_code', sa.Text(), nullable=True),
        sa.Column('session_token', sa.String(64), nullable=False),
        sa.Column('last_active_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('extracted_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('extra_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('consent_ip', sa.String(45), nullable=False),
        sa.Column('gdpr_consent', sa.Boolean(), nullable=False),
        sa.Column('gdpr_consent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('gdpr_consent_version', sa.String(20), nullable=True),
        sa.Column('policy_consent', sa.Boolean(), nullable=False),
        sa.Column('policy_consent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('policy_consent_version', sa.String(20), nullable=True),
        sa.Column('statute_consent', sa.Boolean(), nullable=False),
        sa.Column('statute_consent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('statute_consent_version', sa.String(20), nullable=True),
        sa.Column('keep_me_updated', sa.Boolean(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ref'),
    )

    op.create_table(
        'documents',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('submission_id', sa.UUID(), nullable=False),
        sa.Column('doc_type', sa.Enum('UTILITY_BILL', 'GDPR_FORM', 'POLICY_DOC', 'STATUTE_DOC', 'OTHER', name='documenttype'), nullable=False),
        sa.Column('file_path', sa.String(500), nullable=False),
        sa.Column('original_filename', sa.String(255), nullable=False),
        sa.Column('mime_type', sa.String(100), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['submission_id'], ['submissions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'extractions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('document_id', sa.UUID(), nullable=False),
        sa.Column('extracted_data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('raw_response', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('confirmed_by_user', sa.Boolean(), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('document_id'),
    )

    op.create_table(
        'audit_logs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('action', sa.String(50), nullable=False),
        sa.Column('entity_type', sa.String(50), nullable=False),
        sa.Column('entity_id', sa.String(100), nullable=True),
        sa.Column('ip_address', sa.String(45), nullable=True),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('audit_logs')
    op.drop_table('extractions')
    op.drop_table('documents')
    op.drop_table('submissions')
    op.execute('DROP TYPE IF EXISTS submissionstatus')
    op.execute('DROP TYPE IF EXISTS documenttype')
