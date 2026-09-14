"""a submission remembers its language, and step 1 records the invitation

Approval now asks celine-policies' provisioning service to send the participant
an invitation to set a password, in the participant's language. Two things were
missing for that.

`submissions.locale` is the language the person last used in the wizard. Nothing
stored it before: the only locale on a submission was
`data_sharing_consent_locale`, which is evidence of the language a consent text
was shown in and is empty for everyone who declined the (optional) consent. It
is not backfilled. There is no source to backfill from, and approval falls back
to the REC manifest's language, then to the realm default.

`submission_enablement_steps.invitation` is the provisioning service's reason
code for that invitation (`sent`, `has_password`, `not_on_dev_list`,
`account_disabled`, `not_requested`). It sits beside `detail` rather than inside
it because the console translates the code, and `detail` is an English sentence.
Existing rows stay NULL, which the console reads as "provisioned before
invitations existed" and shows the old `detail` for.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0011'
down_revision: Union[str, None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('submissions', sa.Column('locale', sa.String(8), nullable=True))
    op.add_column(
        'submission_enablement_steps',
        sa.Column('invitation', sa.String(40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('submission_enablement_steps', 'invitation')
    op.drop_column('submissions', 'locale')
