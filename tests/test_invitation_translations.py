"""Every invitation code step 1 can record is translated in every console bundle.

The console shows `admin.invitation.<code>` in place of the English `detail`, and
falls back to the raw code when a key is missing. A code the CLI explains in
English and the console shows as `send_failed` would tell the Italian operator
less than the English one, so the two lists are held together here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from celine.onboarding.services.enablement import INVITATION_DETAIL, RESENDABLE_INVITATIONS

BUNDLES = Path(__file__).resolve().parents[1] / "ui" / "src" / "lib" / "i18n"


@pytest.mark.parametrize("locale", ["it", "en", "es"])
def test_every_code_is_translated(locale):
    bundle = json.loads((BUNDLES / locale / "admin.json").read_text(encoding="utf-8"))
    translated = bundle["invitation"]
    assert set(translated) == set(INVITATION_DETAIL)
    assert all(text.strip() for text in translated.values())


def test_the_codes_the_1_3_0_contract_added_are_listed():
    assert {"cooldown", "send_failed", "no_email"} <= set(INVITATION_DETAIL)


def test_only_a_failed_send_is_resendable():
    assert RESENDABLE_INVITATIONS == {"send_failed"}
