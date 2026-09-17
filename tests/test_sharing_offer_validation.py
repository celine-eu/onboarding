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
    {"id": "household-energy-flexibility", "requires_consent": True, "consent_text_version": "1.0"},
    {"id": "grid-operations-planning", "requires_consent": True, "consent_text_version": "2.0"},
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
        lambda slug: {"consent": {"data_sharing": {"offers": [o["id"] for o in _OFFERS]}}},
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
        await submission_service._validate_sharing_offer_ids("example", ["nope-a", "nope-b"])
    assert "nope-a" in str(exc.value)
    assert "nope-b" in str(exc.value)


async def test_an_unreachable_vocabulary_fails_closed(monkeypatch):
    """Not recorded rather than recorded unchecked.

    The route answers 503, so the client learns the claim is unverified rather
    than wrong, and a retry is the right response.
    """
    monkeypatch.setattr(ts.settings, "ds_ns_url", "")
    monkeypatch.setattr(ts.settings, "ds_connector_url", "")
    monkeypatch.setattr(ts, "load_manifest", lambda slug: {"consent": {"data_sharing": {}}})
    with pytest.raises(ts.SharingOffersUnavailableError):
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


# ── a primary offer gates the others ──────────────────────────────────────────


@pytest.fixture()
def primary(offers, monkeypatch):
    """The same vocabulary, with `grid-operations-planning` declared primary."""
    monkeypatch.setattr(
        ts,
        "load_manifest",
        lambda slug: {
            "consent": {
                "data_sharing": {
                    "offers": [o["id"] for o in _OFFERS],
                    "primary": "grid-operations-planning",
                }
            }
        },
    )


async def test_another_offer_without_the_primary_is_refused(primary):
    with pytest.raises(ValueError, match="depend.*'grid-operations-planning'"):
        await submission_service._validate_sharing_offer_ids(
            "example", ["household-energy-flexibility"]
        )


async def test_another_offer_with_the_primary_is_accepted(primary):
    await submission_service._validate_sharing_offer_ids(
        "example", ["grid-operations-planning", "household-energy-flexibility"]
    )


async def test_the_primary_alone_is_accepted(primary):
    await submission_service._validate_sharing_offer_ids("example", ["grid-operations-planning"])


async def test_accepting_nothing_is_always_allowed(primary):
    """Sharing is optional; a primary gates the others, it never demands itself."""
    await submission_service._validate_sharing_offer_ids("example", [])


async def test_a_primary_that_is_not_published_gates_nothing(offers, monkeypatch):
    monkeypatch.setattr(
        ts,
        "load_manifest",
        lambda slug: {"consent": {"data_sharing": {"primary": "no-such-offer"}}},
    )
    await submission_service._validate_sharing_offer_ids(
        "example", ["household-energy-flexibility"]
    )


# ── the presented set is what the web app trusts ──────────────────────────────

_PRESENTED = [
    {"id": "household-energy-flexibility", "version": "1.0"},
    {"id": "grid-operations-planning", "version": "2.0"},
]


async def test_the_published_set_at_its_versions_is_accepted(offers):
    await submission_service._validate_presented_offers(
        "example", _PRESENTED, ["household-energy-flexibility"]
    )


async def test_presented_and_nothing_accepted_is_accepted(offers):
    """Declining everything is the case the presented set exists for."""
    await submission_service._validate_presented_offers("example", _PRESENTED, [])


async def test_a_presented_version_that_is_not_the_published_one_is_refused(offers):
    """A stale version would silence the question about the current text."""
    stale = [{"id": "household-energy-flexibility", "version": "0.9"}]
    with pytest.raises(ValueError, match="published one is '1.0'"):
        await submission_service._validate_presented_offers("example", stale, None)


@pytest.mark.parametrize("offer_id", ["no-such-offer", "community-incentive-calculation"])
async def test_an_offer_nobody_can_consent_to_was_not_presented(offers, offer_id):
    with pytest.raises(ValueError, match="not a consent-based offer"):
        await submission_service._validate_presented_offers(
            "example", [{"id": offer_id, "version": None}], None
        )


async def test_an_accepted_offer_must_have_been_presented(offers):
    with pytest.raises(ValueError, match="accepted but not presented: grid-operations-planning"):
        await submission_service._validate_presented_offers(
            "example", _PRESENTED[:1], ["household-energy-flexibility", "grid-operations-planning"]
        )


async def test_update_submission_refuses_a_wrong_presented_set_before_writing(offers):
    sub = MagicMock()
    sub.rec_slug = "example"
    sub.statute_consent = False
    sub.data_sharing_consent = False
    sub.data_sharing_offers_presented = None

    update = SubmissionUpdate(
        data_sharing_consent=False,
        data_sharing_consent_offer_ids=[],
        data_sharing_offers_presented=[{"id": "household-energy-flexibility", "version": "0.9"}],
    )

    db = MagicMock()
    with pytest.raises(ValueError, match="Presented data-sharing offers refused"):
        await submission_service.update_submission(db, sub, update)

    assert sub.data_sharing_offers_presented is None
    db.commit.assert_not_called()
