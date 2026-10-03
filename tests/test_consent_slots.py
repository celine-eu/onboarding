"""An applicant is asked to accept only the documents the community declares.

A community that publishes no statute (or no regulations) leaves the slot out of its
manifest's `consent:` block. Nobody can be made to accept a document they cannot read, so
the wizard does not show that checkbox, and the server neither requires nor records it,
whatever a client sends. A slot the community does declare must be accepted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from celine.onboarding.models.schemas import ConsentCreate
from celine.onboarding.services import submission_service, template_service

DOC = {"version": "1.0", "url": "https://legal.example.org/rec-a/privacy/", "required": True}


def _db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


async def _create(data: ConsentCreate, rec_slug: str = "rec-a"):
    db = _db()
    await submission_service.create_from_consent(db, data, "198.51.100.20", rec_slug)
    return db.add.call_args.args[0]


def test_the_asked_slots_are_the_declared_ones(seed_rec):
    seed_rec("rec-a", consent={"gdpr": DOC, "policy": DOC, "data_sharing": {}})
    assert template_service.consent_slots("rec-a") == ("gdpr", "policy")
    seed_rec("rec-b", consent={"gdpr": DOC, "policy": DOC, "statute": DOC})
    assert template_service.consent_slots("rec-b") == ("gdpr", "policy", "statute")
    seed_rec("rec-c")
    assert template_service.consent_slots("rec-c") == ()


async def test_an_undeclared_statute_is_not_asked_and_not_recorded(seed_rec):
    seed_rec("rec-a", consent={"gdpr": DOC, "policy": DOC})
    submission = await _create(ConsentCreate(
        gdpr_consent=True, gdpr_consent_version="1.0", policy_consent=True, policy_consent_version="1.0",
    ))
    assert submission.gdpr_consent and submission.gdpr_consent_version == "1.0"
    assert submission.statute_consent is False
    assert submission.statute_consent_at is None and submission.statute_consent_version is None


async def test_a_statute_sent_for_a_community_without_one_is_ignored(seed_rec):
    seed_rec("rec-a", consent={"gdpr": DOC, "policy": DOC})
    submission = await _create(ConsentCreate(
        gdpr_consent=True, gdpr_consent_version="1.0", policy_consent=True, policy_consent_version="1.0",
        statute_consent=True, statute_consent_version="9.9",
    ))
    assert submission.statute_consent is False and submission.statute_consent_version is None


async def test_a_declared_slot_must_be_accepted(seed_rec):
    seed_rec("rec-a", consent={"gdpr": DOC, "policy": DOC, "statute": DOC})
    with pytest.raises(submission_service.ConsentNotGivenError, match="statute"):
        await _create(ConsentCreate(
            gdpr_consent=True, gdpr_consent_version="1.0", policy_consent=True, policy_consent_version="1.0",
        ))


async def test_the_final_step_cannot_record_an_undeclared_statute(seed_rec):
    from celine.onboarding.models.schemas import SubmissionUpdate

    seed_rec("rec-a", consent={"gdpr": DOC, "policy": DOC})
    current = MagicMock(rec_slug="rec-a", statute_consent=False, data_sharing_consent=False)
    data = SubmissionUpdate(statute_consent=True)
    updates = data.model_dump(exclude_unset=True)
    assert "statute_consent" in updates
    # The guard runs before any write: drive it through the real function up to the
    # first database call and inspect what it would set.
    db = _db()
    db.execute = AsyncMock()
    try:
        await submission_service.update_submission(db, current, data)
    except Exception:
        pass
    assert current.statute_consent is False


def test_submitting_requires_only_the_declared_documents(seed_rec):
    from celine.onboarding.workflows.engine import can_submit

    def _submission(rec_slug, **consents):
        return MagicMock(
            rec_slug=rec_slug, first_name="A", last_name="B", fiscal_code="X", pod_code="IT0",
            email="a@example.org", phone=None, extra_data={}, declared_existing_member=False,
            gdpr_consent=consents.get("gdpr", False), policy_consent=consents.get("policy", False),
            statute_consent=consents.get("statute", False),
        )

    seed_rec("rec-a", consent={"gdpr": DOC, "policy": DOC})
    assert can_submit(_submission("rec-a", gdpr=True, policy=True)) == []
    assert can_submit(_submission("rec-a", gdpr=True)) == ["Policy consent is required"]
    seed_rec("rec-b", consent={"gdpr": DOC, "policy": DOC, "statute": DOC})
    assert can_submit(_submission("rec-b", gdpr=True, policy=True)) == ["Statute consent is required"]
