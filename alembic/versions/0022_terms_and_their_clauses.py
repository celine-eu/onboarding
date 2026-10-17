"""the terms of use, and the specific approval of their clauses, each recorded on its own

`submissions.terms_consent{,_at,_version,_url,_sha256}` and the same for `terms_clauses`:
two acts on one document, each with its own box and time (Art. 1341(2) c.c.), recorded as
0020 records every other slot. Existing rows were never asked: false and NULL.

`ix_enablement_step_external_ref`: the member's own acceptances (`/api/me/acceptances`) find
their application through the login step's Keycloak uuid.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-17

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SLOTS = ("terms", "terms_clauses")


def upgrade() -> None:
    for slot in SLOTS:
        op.add_column(
            "submissions",
            sa.Column(f"{slot}_consent", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.add_column(
            "submissions",
            sa.Column(f"{slot}_consent_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.add_column(
            "submissions", sa.Column(f"{slot}_consent_version", sa.String(20), nullable=True)
        )
        op.add_column("submissions", sa.Column(f"{slot}_consent_url", sa.Text(), nullable=True))
        op.add_column(
            "submissions", sa.Column(f"{slot}_consent_sha256", sa.String(64), nullable=True)
        )
    op.create_index(
        "ix_enablement_step_external_ref",
        "submission_enablement_steps",
        ["step", "external_ref"],
    )


def downgrade() -> None:
    op.drop_index("ix_enablement_step_external_ref", table_name="submission_enablement_steps")
    for slot in reversed(SLOTS):
        for column in ("sha256", "url", "version", "at"):
            op.drop_column("submissions", f"{slot}_consent_{column}")
        op.drop_column("submissions", f"{slot}_consent")
