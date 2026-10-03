"""each accepted document records the page shown and its hash

`submissions.{gdpr,policy,statute}_consent_url` and `..._sha256`: where the document the
applicant accepted was served, and the sha256 of that page, as the legal host published it
(`services/legal_documents.py`). With the version, they say exactly which text was accepted.

Existing rows are NULL: what they were shown was not recorded.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-03

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SLOTS = ("gdpr", "policy", "statute")


def upgrade() -> None:
    for slot in SLOTS:
        op.add_column("submissions", sa.Column(f"{slot}_consent_url", sa.Text(), nullable=True))
        op.add_column("submissions", sa.Column(f"{slot}_consent_sha256", sa.String(64), nullable=True))


def downgrade() -> None:
    for slot in reversed(SLOTS):
        op.drop_column("submissions", f"{slot}_consent_sha256")
        op.drop_column("submissions", f"{slot}_consent_url")
