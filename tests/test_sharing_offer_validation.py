"""Phase 0 — offer ids are checked before they are recorded as a consent.

The ids arrive from the client. Before this check they were stored on the sender's
word, and the connector only disagreed days later, at provisioning: a 409 for a
contract-based offer, a 422 for an unknown one. Both surfaced as
``share_provisioned = false``, which reads as *this member chose not to share*.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

import celine.onboarding.services.template_service as ts
from celine.onboarding.models.schemas import SubmissionUpdate
from celine.onboarding.services import submission_service

_OriginalAsyncClient = httpx.AsyncClient

_OFFERS = [
    {"id": "household-energy-flexibility", "requires_consent": True},
    {"id": "grid-operations-planning", "requires_consent": True},
    # Disclosed, never consented. A manifest allow-list may legitimately name it.
    {"id": "community-incentive-calculation", "requires_consent": False},
]


@pytest.fixture()
def offers(monkeypatch):
    """Serve the three-offer vocabulary, allow-listing all of them.

    The allow-list is what lets the contract-based offer through
    `get_sharing_offers` — without it the default filter drops it and the
    interesting case cannot be reached.
    """
    monkeypatch.setattr(ts.settings, "ds_ns_url", "http://connector:30001")
    monkeypatch.setattr(
        ts,
        "load_manifest",
        lambda slug: {
            "consent": {"data_sharing": {"offers": [o["id"] for o in _OFFERS]}}
        },
    )
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=_OFFERS))

    def factory(**kw):
        kw.pop("transport", None)
        return _OriginalAsyncClient(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_a_consent_based_offer_is_accepted(offers):
    await submission_service._validate_sharing_offer_ids(
        "example", ["household-energy-flexibility"]
    )


async def test_several_consent_based_offers_are_accepted(offers):
    """Consent is purpose-scoped, so one person holding several is ordinary."""
    await submission_service._validate_sharing_offer_ids(
        "example", ["household-energy-flexibility", "grid-operations-planning"]
    )


async def test_an_unknown_offer_is_refused(offers):
    with pytest.raises(ValueError, match="Unknown data-sharing offer"):
        await submission_service._validate_sharing_offer_ids(
            "example", ["household-energy-flexibility", "no-such-offer"]
        )


async def test_a_contract_based_offer_is_refused(offers):
    """`POST /consent/admin/shares` answers 409 for this — verified against a
    running connector on 2026-08-27. Refusing here is the same judgement, taken
    while the person is still in front of the wizard."""
    with pytest.raises(ValueError, match="disclosed, not consented"):
        await submission_service._validate_sharing_offer_ids(
            "example", ["community-incentive-calculation"]
        )


async def test_the_message_names_every_bad_id_not_just_the_first(offers):
    """An operator retyping one id at a time is how a two-line fix takes a day."""
    with pytest.raises(ValueError) as exc:
        await submission_service._validate_sharing_offer_ids(
            "example", ["nope-a", "nope-b"]
        )
    assert "nope-a" in str(exc.value)
    assert "nope-b" in str(exc.value)


async def test_an_unreachable_vocabulary_fails_closed(monkeypatch):
    """Not recorded rather than recorded unchecked.

    The route answers 503, so the client learns the claim is unverified rather
    than wrong, and a retry is the right response.
    """
    monkeypatch.setattr(ts.settings, "ds_ns_url", "")
    monkeypatch.setattr(ts.settings, "ds_connector_url", "")
    monkeypatch.setattr(
        ts, "load_manifest", lambda slug: {"consent": {"data_sharing": {}}}
    )
    with pytest.raises(ts.SharingOffersUnavailable):
        await submission_service._validate_sharing_offer_ids("example", ["anything"])


async def test_update_submission_refuses_before_it_mutates_anything(offers):
    """The wiring, and the ordering that makes a refusal safe."""
    sub = MagicMock()
    sub.rec_slug = "example"
    sub.statute_consent = False
    sub.data_sharing_consent = False

    update = SubmissionUpdate(
        data_sharing_consent=True,
        data_sharing_consent_offer_ids=["community-incentive-calculation"],
        data_sharing_consent_text_version="1.0",
        data_sharing_consent_text_sha256="a" * 64,
        data_sharing_consent_locale="it",
    )

    db = MagicMock()
    with pytest.raises(ValueError, match="disclosed, not consented"):
        await submission_service.update_submission(db, sub, update)

    # Nothing was written and the session was never touched: a refused consent
    # must not leave a half-updated submission behind.
    assert sub.data_sharing_consent is False
    db.commit.assert_not_called()
