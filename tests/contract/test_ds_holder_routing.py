"""An offer is read where it is held — the ds behaviour the POD export's routing rests on.

ADR-0008. The export reads an offer's audience at every connector the REC's
manifest routes it to, and reads a connector's ``422`` as "this connector holds no
dataset for the offer" — an answer, not a failure — so an offer held entirely by
another participant is read there and not refused at the community's own
connector. These check that ds still behaves that way, and that this service's
client may read the audience at a connector that is not the community's.

It also checks the decisions list the export's withdrawn column is read from
(``GET /consent/admin/decisions``, ds ADR-0021): answered at the holder to the
community's own organisation client (``DS_CONTRACT_ORG_CLIENT_*``), refused to
this service's client, and ``422`` where the offer is not held.

**Read-only, like the rest of the suite.** No export is pressed and nothing is
written. (The export records no disclosure since ADR-0010, so there is no
disclosure route to check at the holder.)

Needs ``DS_CONTRACT_HOLDER_CONNECTOR_URL`` and ``DS_CONTRACT_HOLDER_OFFER`` — an
offer whose data that connector holds and ``DS_CONTRACT_CONNECTOR_URL`` does not.
Deselected, and said so in the summary, while both are unset.
"""

from __future__ import annotations

import httpx
import pytest

import celine.onboarding.services.dataspace_identity as di

from .conftest import (
    CLIENT_ID,
    CONNECTOR_URL,
    HOLDER,
    HOLDER_CONNECTOR_URL,
    HOLDER_OFFER,
    IR_URL,
    skip_unconfigured,
)

pytestmark = [pytest.mark.ds_contract, pytest.mark.needs_holder]


@pytest.fixture(autouse=True)
def _configured(specs):
    skip_unconfigured(HOLDER, "a connector holding an offer the community's does not")


@pytest.fixture(scope="module")
def recipient_did(auth) -> str:
    """The offer's recipient, as the export resolves it: vocabulary, then registry."""
    offers = httpx.get(f"{CONNECTOR_URL}/ns/sharing-offers", timeout=10).json()
    offer = next((o for o in offers if o["id"] == HOLDER_OFFER), None)
    assert offer, f"{HOLDER_OFFER!r} is not in the published vocabulary"
    recipients = offer.get("recipients") or {}
    alias = recipients.get("recipient") or recipients.get("controller")
    owner = httpx.get(f"{IR_URL}/owners/resolve", params={"alias": alias}, headers=auth, timeout=10)
    assert owner.status_code == 200, f"{alias!r} does not resolve: {owner.status_code}"
    did = owner.json().get("did")
    assert did, f"{alias!r} holds no DID"
    return did


def test_the_holder_answers_the_audience_of_an_offer_it_holds(auth, recipient_did):
    """This service's client reads the audience at another participant's
    connector, and it comes back per dataset — the shape the agreement check reads."""
    r = httpx.get(
        f"{HOLDER_CONNECTOR_URL}/consent/admin/shares",
        params={"offer_id": HOLDER_OFFER, "consumer_id": recipient_did},
        headers=auth,
        timeout=10,
    )
    assert r.status_code != 403, (
        f"403 from the holder's GET /consent/admin/shares — {CLIENT_ID} may not read "
        "the audience there, and an offer held only by it cannot be exported"
    )
    assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
    datasets = r.json().get("datasets")
    assert isinstance(datasets, list) and datasets and "subject_ids" in datasets[0]


def test_a_connector_holding_nothing_for_the_offer_answers_422(auth, recipient_did):
    """The answer the export reads as "holds nothing here". A 200 with an empty
    list, or a 403, would change what a route holding nothing means."""
    r = httpx.get(
        f"{CONNECTOR_URL}/consent/admin/shares",
        params={"offer_id": HOLDER_OFFER, "consumer_id": recipient_did},
        headers=auth,
        timeout=10,
    )
    assert r.status_code == 422, (
        f"expected 422 from the community's connector, which holds no dataset for "
        f"{HOLDER_OFFER!r}; got {r.status_code}: {r.text[:200]}. If it now holds one, "
        "DS_CONTRACT_HOLDER_OFFER no longer names an offer held only elsewhere."
    )


async def test_the_export_reads_the_offer_where_it_is_held(auth, recipient_did, monkeypatch):
    """This service's own read, end to end against ds: routed to both connectors,
    the community's answers that it holds nothing, and the audience is the holder's."""

    async def _headers():
        return dict(auth)

    monkeypatch.setattr(di, "_auth_headers", _headers)
    own = di.ConsentRoute(offer_id=HOLDER_OFFER, connector_url=CONNECTOR_URL, collector="probe")
    holder = di.ConsentRoute(
        offer_id=HOLDER_OFFER,
        connector_url=HOLDER_CONNECTOR_URL,
        collector="probe",
        holder="holder",
    )

    audience = await di.get_offer_audience(HOLDER_OFFER, recipient_did, [own, holder])

    assert audience.routes == (holder,)
    assert audience.datasets


# ── who withdrew: `GET /consent/admin/decisions` (ds ADR-0021, this ADR-0009) ──


def _decisions(base: str, headers: dict[str, str], **params) -> httpx.Response:
    return httpx.get(
        f"{base}/consent/admin/decisions",
        params={"offer_id": HOLDER_OFFER, **params},
        headers=headers,
        timeout=15,
    )


def test_the_holder_lists_the_communitys_decisions_to_its_own_client(org_auth):
    """The organisation client may list its members' decisions at the holder, and
    the list ends only on a null cursor. Read to the end; nothing is written."""
    cursor = None
    for _ in range(1000):
        r = _decisions(
            HOLDER_CONNECTOR_URL, org_auth, limit=100, **({"cursor": cursor} if cursor else {})
        )
        assert r.status_code not in (404, 405), (
            "the holder serves no GET /consent/admin/decisions — an older ds; the POD "
            "export marks its withdrawals NOT reported there"
        )
        assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
        body = r.json()
        assert {"offer_id", "datasets", "subjects", "next_cursor"} <= body.keys()
        for subject in body["subjects"]:
            for decision in subject["decisions"]:
                assert decision["state"] in ("granted", "withdrawn")
        cursor = body["next_cursor"]
        if cursor is None:
            return
    pytest.fail("the decisions list did not end within 1000 pages")


def test_the_decisions_list_refuses_this_services_own_client(auth):
    """Why the export reads it as the community: the service client is a 403."""
    r = _decisions(HOLDER_CONNECTOR_URL, auth, limit=1)
    assert r.status_code == 403, (
        f"expected 403 for {CLIENT_ID} on the decisions list, got {r.status_code}: "
        f"{r.text[:200]}. If it is admitted, the list is no longer bounded to the "
        "collector's own members."
    )


def test_a_connector_holding_nothing_answers_the_decisions_list_422(org_auth):
    """The same "holds nothing here" answer as the audience read."""
    r = _decisions(CONNECTOR_URL, org_auth, limit=1)
    assert r.status_code == 422, f"{r.status_code}: {r.text[:200]}"


async def test_the_export_reads_withdrawals_where_the_offer_is_held(org_auth, monkeypatch):
    """This service's own read, end to end against ds, as the organisation."""

    async def _headers(alias):
        return dict(org_auth)

    monkeypatch.setattr(di, "organisation_auth_headers", _headers)
    holder = di.ConsentRoute(
        offer_id=HOLDER_OFFER,
        connector_url=HOLDER_CONNECTOR_URL,
        collector="probe",
        holder="holder",
    )

    decisions = await di.get_offer_decisions(HOLDER_OFFER, [holder])

    assert decisions.unreported == ()
