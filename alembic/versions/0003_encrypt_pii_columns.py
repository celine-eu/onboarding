"""encrypt PII columns

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # VARCHAR -> TEXT (metadata-only in PostgreSQL, no table rewrite)
    op.alter_column('submissions', 'first_name',
                    type_=sa.Text(), existing_type=sa.String(100))
    op.alter_column('submissions', 'last_name',
                    type_=sa.Text(), existing_type=sa.String(100))
    op.alter_column('submissions', 'consent_ip',
                    type_=sa.Text(), existing_type=sa.String(45))

    # JSONB -> TEXT (cast existing JSON values to text)
    op.alter_column('submissions', 'extracted_data',
                    type_=sa.Text(),
                    existing_type=postgresql.JSONB(),
                    postgresql_using='extracted_data::text')
    op.alter_column('submissions', 'id_extracted_data',
                    type_=sa.Text(),
                    existing_type=postgresql.JSONB(),
                    postgresql_using='id_extracted_data::text')
    op.alter_column('extractions', 'extracted_data',
                    type_=sa.Text(),
                    existing_type=postgresql.JSONB(),
                    postgresql_using='extracted_data::text')
    op.alter_column('extractions', 'raw_response',
                    type_=sa.Text(),
                    existing_type=postgresql.JSONB(),
                    postgresql_using='raw_response::text')


def downgrade() -> None:
    op.alter_column('submissions', 'first_name',
                    type_=sa.String(100), existing_type=sa.Text())
    op.alter_column('submissions', 'last_name',
                    type_=sa.String(100), existing_type=sa.Text())
    op.alter_column('submissions', 'consent_ip',
                    type_=sa.String(45), existing_type=sa.Text())

    # TEXT -> JSONB downgrade only works for unencrypted rows
    op.alter_column('submissions', 'extracted_data',
                    type_=postgresql.JSONB(),
                    existing_type=sa.Text(),
                    postgresql_using='extracted_data::jsonb')
    op.alter_column('submissions', 'id_extracted_data',
                    type_=postgresql.JSONB(),
                    existing_type=sa.Text(),
                    postgresql_using='id_extracted_data::jsonb')
    op.alter_column('extractions', 'extracted_data',
                    type_=postgresql.JSONB(),
                    existing_type=sa.Text(),
                    postgresql_using='extracted_data::jsonb')
    op.alter_column('extractions', 'raw_response',
                    type_=postgresql.JSONB(),
                    existing_type=sa.Text(),
                    postgresql_using='raw_response::jsonb')
