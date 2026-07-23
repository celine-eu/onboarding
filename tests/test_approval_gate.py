from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from celine.onboarding.services import submission_service, template_service


def _submission(phone_verified: bool):
    sub = MagicMock()
    sub.rec_slug = "example"
    sub.phone_verified = phone_verified
    return sub


def test_gate_blocks_unverified_when_step_present(monkeypatch):
    monkeypatch.setattr(
        template_service, "load_manifest", lambda slug: {"steps": ["personal", "phone_verify"]}
    )
    with pytest.raises(ValueError, match="phone number is not verified"):
        submission_service._assert_phone_verified(_submission(False))


def test_gate_allows_verified_when_step_present(monkeypatch):
    monkeypatch.setattr(
        template_service, "load_manifest", lambda slug: {"steps": ["personal", "phone_verify"]}
    )
    submission_service._assert_phone_verified(_submission(True))  # no raise


def test_gate_noop_when_step_absent(monkeypatch):
    """RECs that never opted into SMS verification approve as before."""
    monkeypatch.setattr(
        template_service, "load_manifest", lambda slug: {"steps": ["personal", "review"]}
    )
    submission_service._assert_phone_verified(_submission(False))  # no raise
