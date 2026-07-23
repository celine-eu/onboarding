from __future__ import annotations

from datetime import datetime, timezone

from celine.onboarding.outputs import csv_export


def test_fmt_none_is_empty_string():
    assert csv_export._fmt(None) == ""


def test_fmt_datetime_is_isoformat():
    dt = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)
    assert csv_export._fmt(dt) == dt.isoformat()


def test_fmt_bool_and_str():
    assert csv_export._fmt(True) == "True"
    assert csv_export._fmt("x") == "x"


def test_base_fields_cover_dataspace_and_phone_and_consent_detail():
    """3A.1 + 3A.2: dataspace identity, phone verification, and consent
    timestamps/versions must all be exportable."""
    fields = set(csv_export.BASE_FIELDS)
    assert {"dataspace_did", "dataspace_subject_id"} <= fields
    assert {"phone_verified", "phone_verified_at"} <= fields
    for base in ("gdpr", "policy", "statute"):
        assert {f"{base}_consent", f"{base}_consent_at", f"{base}_consent_version"} <= fields
