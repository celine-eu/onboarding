"""A data-sharing consent is only defensible with proof of what was shown.

The connector requires `consent_text_version` and `rendered_text_sha256` on every
grant and refuses one without them. Share provisioning is deliberately non-fatal,
so an incomplete consent used to produce a 422 that left `share_provisioned=false`
and surfaced only in a log: the person consented, the record was refused, and
nothing said so. Retrying never helped either — you cannot retrospectively prove
what somebody was shown.

So the check belongs at capture, with a pre-flight before provisioning and an
explanation on the review screen.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import celine.onboarding.services.dataspace_identity as di
from celine.onboarding.models.schemas import SubmissionAdminRead, SubmissionUpdate

COMPLETE = {
    "data_sharing_consent": True,
    "data_sharing_consent_offer_ids": ["household-energy-flexibility"],
    "data_sharing_consent_text_version": "1.0",
    "data_sharing_consent_text_sha256": "a" * 64,
    "data_sharing_consent_locale": "it",
}


# ── capture ───────────────────────────────────────────────────────────────────


def test_complete_consent_is_accepted():
    assert SubmissionUpdate(**COMPLETE).data_sharing_consent is True


@pytest.mark.parametrize(
    "field",
    [
        "data_sharing_consent_text_version",
        "data_sharing_consent_text_sha256",
        "data_sharing_consent_locale",
    ],
)
def test_missing_evidence_is_refused(field):
    payload = {**COMPLETE, field: None}
    with pytest.raises(ValidationError, match="evidence of what was shown"):
        SubmissionUpdate(**payload)


@pytest.mark.parametrize(
    "field",
    [
        "data_sharing_consent_text_version",
        "data_sharing_consent_text_sha256",
        "data_sharing_consent_locale",
    ],
)
def test_blank_evidence_is_refused(field):
    """Empty is not a value. The connector rejects it too."""
    payload = {**COMPLETE, field: "   "}
    with pytest.raises(ValidationError, match="evidence of what was shown"):
        SubmissionUpdate(**payload)


def test_consent_without_offers_is_refused():
    payload = {**COMPLETE, "data_sharing_consent_offer_ids": []}
    with pytest.raises(ValidationError, match="must name the offers"):
        SubmissionUpdate(**payload)


def test_several_offer_versions_fit():
    """Consent is purpose-scoped, so several offers over one dataset is the
    intended shape — the wizard comma-joins their deduplicated versions. The
    column was sized for the single-offer case and overflowed at four, failing
    the submission at the last step for a reason nothing in the UI could explain.
    """
    versions = ",".join(f"{n}.0" for n in range(1, 21))  # 80 chars
    SubmissionUpdate(**{**COMPLETE, "data_sharing_consent_text_version": versions})


def test_declining_needs_no_evidence():
    """Withdrawal and refusal must never be harder than agreeing."""
    SubmissionUpdate(data_sharing_consent=False)


def test_unrelated_update_is_untouched():
    SubmissionUpdate(first_name="Alice")


# ── pre-flight ────────────────────────────────────────────────────────────────


def _sub(**overrides):
    base = dict(
        ref="20260713-abcd",
        rec_slug="example",
        dataspace_did="did:web:users.example:email-abc",
        data_sharing_consent=True,
        data_sharing_consent_offer_ids=["household-energy-flexibility"],
        data_sharing_consent_text_version="1.0",
        data_sharing_consent_text_sha256="a" * 64,
        data_sharing_consent_locale="it",
        data_sharing_consent_at=None,
        share_provisioned=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_complete_evidence_has_no_problems():
    assert di._evidence_problems(_sub()) == []


@pytest.mark.parametrize(
    "field,expected",
    [
        ("data_sharing_consent_text_version", "consent text version"),
        ("data_sharing_consent_text_sha256", "rendered text hash"),
    ],
)
def test_pre_flight_names_the_missing_field(field, expected):
    problems = di._evidence_problems(_sub(**{field: None}))
    assert any(expected in p for p in problems)


@pytest.mark.parametrize("field", ["ref", "rec_slug"])
def test_pre_flight_catches_an_email_used_as_a_reference(field):
    """The commonest way personal data leaks into a codes-only store."""
    problems = di._evidence_problems(_sub(**{field: "user@example.com"}))
    assert any("email address" in p for p in problems)


async def test_provisioning_refuses_before_posting(monkeypatch):
    """Do not send a record the connector will reject; say why locally."""
    monkeypatch.setattr(di.settings, "ds_connector_url", "http://connector:30001")
    posted = []

    async def _fail(*a, **kw):
        posted.append(a)
        raise AssertionError("should not have posted")

    monkeypatch.setattr(di, "_auth_headers", _fail)

    sub = _sub(data_sharing_consent_text_sha256=None)
    assert await di.provision_user_shares(sub) is False
    assert posted == []


async def test_provisioning_raises_on_retry(monkeypatch):
    """An operator retrying explicitly wants to see the failure."""
    monkeypatch.setattr(di.settings, "ds_connector_url", "http://connector:30001")

    sub = _sub(data_sharing_consent_text_version=None)
    with pytest.raises(ValueError, match="evidence is incomplete"):
        await di.provision_user_shares(sub, raise_on_error=True)


# ── the review screen ─────────────────────────────────────────────────────────


def _admin_read(**overrides):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    fields = dict(
        id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        ref="20260713-abcd",
        rec_slug="example",
        status="submitted",
        first_name=None,
        last_name=None,
        email=None,
        phone=None,
        fiscal_code=None,
        pod_code=None,
        supply_municipality=None,
        extracted_data=None,
        id_extracted_data=None,
        extra_data=None,
        gdpr_consent=True,
        gdpr_consent_at=now,
        gdpr_consent_version="1.0",
        policy_consent=True,
        policy_consent_at=now,
        policy_consent_version="1.0",
        statute_consent=True,
        statute_consent_at=now,
        statute_consent_version="1.0",
        data_sharing_consent=True,
        data_sharing_consent_at=now,
        data_sharing_consent_offer_ids=["household-energy-flexibility"],
        data_sharing_consent_text_version="1.0",
        data_sharing_consent_locale="it",
        data_sharing_consent_text_sha256="a" * 64,
        share_provisioned=True,
        keep_me_updated=False,
        phone_verified=True,
        phone_verified_at=now,
        notes=None,
        created_at=now,
        updated_at=now,
        consent_ip="127.0.0.1",
        dataspace_subject_id=None,
        dataspace_did=None,
        dataspace_vc_id=None,
        dataspace_vc_issued_at=None,
    )
    fields.update(overrides)
    return SubmissionAdminRead(**fields)


def test_review_explains_a_permanent_evidence_gap():
    """Naming the cause matters: this class of failure survives every retry."""
    read = _admin_read(share_provisioned=False, data_sharing_consent_text_version=None)
    assert any("cannot be repaired" in issue for issue in read.data_sharing_issues)


def test_review_flags_a_retriable_failure_differently():
    read = _admin_read(share_provisioned=False)
    assert read.data_sharing_issues == [
        "Consent recorded but not yet provisioned to the dataspace. "
        "Retry from the admin action once the cause is resolved."
    ]


def test_review_is_quiet_when_provisioned():
    assert _admin_read().data_sharing_issues == []


def test_review_is_quiet_without_a_sharing_consent():
    assert (
        _admin_read(data_sharing_consent=False, share_provisioned=False).data_sharing_issues == []
    )
