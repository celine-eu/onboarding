"""a submission remembers which sharing offers the wizard presented

`submissions.data_sharing_offers_presented` holds every consent-based offer the
statute step showed, accepted or not, as `[{"id", "version"}]`. The accepted
ids alone cannot tell "shown and declined" from "never shown", so the web app
had either to re-ask every decline or to miss offers added later. With this it
asks only about an offer that is new, or whose version moved, since the form.

Nothing is backfilled: what an earlier wizard showed was not recorded, and a
guess would be a claim about a person. Those members are asked once in the web
app, which is what happened before.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0013'
down_revision: Union[str, None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'submissions',
        sa.Column('data_sharing_offers_presented', postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('submissions', 'data_sharing_offers_presented')
