"""Half two — the things a schema cannot say.

Two of the three failures this plan repairs were invisible to OpenAPI:

- `GET /admin/owners/{owner_id}` matched on **id**, and this service passed an
  **alias**. The path template and the method were both exactly as published; a
  404 was the only way to find out.
- `POST /consent/admin/shares` refuses a contract-based offer with 409. That is a
  rule about `requires_consent`, not a constraint any schema carries.

So these call ds and assert behaviour. They are read-only or deliberately
invalid: nothing here creates a participant, issues a credential or records a
disclosure, because a check that mutates a shared dev stack gets switched off.
"""

from __future__ import annotations

import httpx
import pytest

from .conftest import CONNECTOR_URL, IR_URL

pytestmark = pytest.mark.ds_contract


@pytest.fixture(autouse=True)
def _ds_is_up(specs):
    """Skip, do not fail, when ds is not there.

    These call ds directly rather than through a fixture, so without this an
    unreachable stack surfaces as a `ConnectError` failure — and a red suite that
    means "nothing was checked" is the noise that gets a check deleted. Depending
    on `specs` reuses the schema half's reachability probe, so both halves skip on
    one signal and say the same thing.
    """


# Seeded by the ds dev fixtures: an owner whose id and alias differ, which is the
# whole point — the two are indistinguishable on any deployment where they match.
OWNER_ID = "example-org"
OWNER_ALIAS = "example"

CONSENT_OFFER = "household-energy-flexibility"
CONTRACT_OFFER = "community-incentive-calculation"


def test_owners_resolve_accepts_an_alias(auth):
    """The bug Phase 1 fixed, asserted from the outside.

    A manifest's `organization:` holds an alias. If this ever 404s again, this
    service refuses to start on a deployment that is configured correctly.
    """
    r = httpx.get(
        f"{IR_URL}/owners/resolve", params={"alias": OWNER_ALIAS}, headers=auth, timeout=10
    )
    assert r.status_code == 200, (
        f"/owners/resolve no longer resolves the alias {OWNER_ALIAS!r} "
        f"({r.status_code}); check_organization would read that as 'no such "
        f"organisation' and refuse to boot"
    )
    assert r.json().get("id") == OWNER_ID


def test_owners_resolve_reports_a_lifecycle_status(auth):
    """Phase 2 refuses a non-verified organisation, so the field has to be there.

    An absent status is treated as "unknown, carry on" by design — which means
    ds dropping the field would silently disable the check rather than break it.
    """
    r = httpx.get(
        f"{IR_URL}/owners/resolve", params={"alias": OWNER_ALIAS}, headers=auth, timeout=10
    )
    assert r.status_code == 200
    assert r.json().get("status"), (
        "no `status` on the resolved owner — the suspended/revoked check in "
        "main.py would pass silently for every organisation"
    )


def test_the_admin_route_still_does_not_accept_an_alias(auth):
    """The control that keeps the test above meaningful.

    If `/admin/owners/{owner_id}` ever starts accepting aliases the distinction
    disappears, and so does the reason this service moved off it. Then this test
    fails and somebody reads why, rather than the pair quietly becoming the same
    assertion.
    """
    r = httpx.get(f"{IR_URL}/admin/owners/{OWNER_ALIAS}", headers=auth, timeout=10)
    assert r.status_code == 404, (
        f"/admin/owners/{{owner_id}} answered {r.status_code} for an alias — the "
        "id/alias distinction may have changed; re-read Phase 1 before relying on it"
    )


def test_a_contract_offer_cannot_be_provisioned_as_consent(auth):
    """The rule Phase 0 enforces at capture, verified at its source.

    Deliberately invalid: the subject does not exist, so nothing is created
    whichever way this goes. The 409 must arrive *before* any of that matters.
    """
    r = httpx.post(
        f"{CONNECTOR_URL}/consent/admin/shares",
        headers=auth,
        timeout=10,
        json={
            "subject_id": "did:web:rec.dataspaces.localhost:users:contract-probe",
            "offer_id": CONTRACT_OFFER,
            "enabled": True,
            "legal_basis": {
                "source": "onboarding-contract-check",
                "consent_text_version": "0",
                "rendered_text_sha256": "0" * 64,
            },
        },
    )
    assert r.status_code == 409, (
        f"expected 409 for the contract-based offer {CONTRACT_OFFER!r}, got "
        f"{r.status_code}: {r.text[:200]}. Phase 0 rejects these at capture on "
        f"the strength of this rule."
    )


def test_the_disclosure_route_is_ours_to_call(auth):
    """Scope, not schema.

    `connector.disclosure.record` is on this service's client. A 403 here would
    mean the grant went, and every POD export would stop — so the assertion is
    that we get the *dataset* complaint, not the permission one.

    The offer id is deliberately nonsense, so nothing is recorded.
    """
    r = httpx.post(
        f"{CONNECTOR_URL}/admin/disclosure",
        headers=auth,
        timeout=10,
        json={"offer_id": "no-such-offer-contract-check", "recipient_ref": "probe"},
    )
    assert r.status_code != 403, (
        "403 from /admin/disclosure — svc-ds-onboarding has lost "
        "connector.disclosure.record, and every POD export now fails"
    )
    assert r.status_code == 422, (
        f"expected 422 naming the unknown offer, got {r.status_code}: {r.text[:200]}"
    )


def test_a_consent_offer_is_still_published_and_consent_based(auth):
    """Phase 0 validates recorded ids against this vocabulary."""
    r = httpx.get(f"{CONNECTOR_URL}/ns/sharing-offers", timeout=10)
    assert r.status_code == 200
    offers = {o["id"]: o for o in r.json()}
    assert CONSENT_OFFER in offers, (
        f"{CONSENT_OFFER!r} is gone from the published vocabulary; every "
        "submission naming it would now be refused at capture"
    )
    assert offers[CONSENT_OFFER]["requires_consent"] is True
    assert offers[CONTRACT_OFFER]["requires_consent"] is False


# ── reading the consent plane ─────────────────────────────────────


def test_the_resolved_owner_carries_a_dataspace_identifier(auth):
    """The alias-to-DID mapping the POD export depends on.

    A sharing offer names its controller by alias; the consent plane is keyed by
    DID. This route is the only place the two meet, so if `did` disappears from
    the response the export cannot name a recipient and no other service can
    supply one without inventing a second mapping.
    """
    r = httpx.get(
        f"{IR_URL}/owners/resolve", params={"alias": OWNER_ALIAS}, headers=auth, timeout=10
    )
    assert r.status_code == 200
    assert "did" in r.json(), (
        "no `did` on the resolved owner — the POD export cannot resolve an "
        "offer's controller to the identifier the consent plane is keyed by"
    )


def test_the_audience_read_is_ours_to_call(auth):
    """Scope, not schema.

    `connector.consent.audience` is on this service's client, granted for this
    one call. A 403 would mean the grant went and the POD export falls back to
    reading a form — which is the defect it exists to fix, so it must fail
    loudly rather than degrade.

    The offer id is deliberately nonsense, so the answer is about the offer.
    """
    r = httpx.get(
        f"{CONNECTOR_URL}/consent/admin/shares",
        params={"offer_id": "no-such-offer-contract-check", "consumer_id": "did:web:probe"},
        headers=auth,
        timeout=10,
    )
    assert r.status_code != 403, (
        "403 from GET /consent/admin/shares — svc-ds-onboarding has lost "
        "connector.consent.audience, and the POD export can no longer read who consents"
    )
    assert r.status_code == 422, (
        f"expected 422 naming the unknown offer, got {r.status_code}: {r.text[:200]}"
    )


def test_the_audience_read_refuses_the_wildcard_consumer(auth):
    """The refusal this caller relies on, asserted at its source.

    The standing rows this service writes are wildcard-scoped, and a per-party
    opt-out beats the standing wildcard. Reading *as* the wildcard would load
    only the standing rows and return people who have specifically opted out of
    the recipient — a disclosure against a withdrawn consent. The connector
    refusing it is what makes naming the recipient non-optional here.
    """
    r = httpx.get(
        f"{CONNECTOR_URL}/consent/admin/shares",
        params={"offer_id": CONSENT_OFFER, "consumer_id": "*"},
        headers=auth,
        timeout=10,
    )
    assert r.status_code == 422, (
        f"expected 422 for the wildcard consumer, got {r.status_code}: "
        f"{r.text[:200]}. If this ever succeeds, an export could go out to a "
        f"recipient somebody had specifically opted out of."
    )


def test_a_contract_offer_has_no_audience_to_read(auth):
    """Disclosed, not consented — the same rule the write side enforces.

    The two paths are meant to agree about which offers carry a decision, and
    the POD export surfaces this as a caller error rather than an empty list:
    "nobody consents" and "nothing was asked" must not look alike.
    """
    r = httpx.get(
        f"{CONNECTOR_URL}/consent/admin/shares",
        params={"offer_id": CONTRACT_OFFER, "consumer_id": "did:web:probe"},
        headers=auth,
        timeout=10,
    )
    assert r.status_code == 409, (
        f"expected 409 for the contract-based offer {CONTRACT_OFFER!r}, got "
        f"{r.status_code}: {r.text[:200]}"
    )


def test_the_whole_recipient_chain_resolves(auth):
    """Offer to controller to DID to audience, in the order the export walks it.

    Each hop is published and none of them is inferred: the offer names its
    controller by alias, the registry maps that alias to the identifier the
    consent plane is keyed by, and the consent plane answers for it. If any hop
    stops working the export cannot name a recipient, and the failure is worth
    seeing here rather than at the moment somebody exports.

    It also asserts the shape the export refuses to flatten — one subject set
    per dataset, never merged. A caller reading the first element is correct
    until a second dataset declares the same offer, and then silently wrong.
    """
    offers = httpx.get(f"{CONNECTOR_URL}/ns/sharing-offers", timeout=10).json()
    offer = next(o for o in offers if o["id"] == CONSENT_OFFER)
    controller = (offer.get("recipients") or {}).get("controller")
    assert controller, (
        f"{CONSENT_OFFER!r} names no controller — the export has nothing to "
        "resolve a recipient from, and must not guess one"
    )

    owner = httpx.get(
        f"{IR_URL}/owners/resolve", params={"alias": controller}, headers=auth, timeout=10
    )
    assert owner.status_code == 200, (
        f"the offer's controller {controller!r} does not resolve in the registry "
        f"({owner.status_code}); the export would refuse to name a recipient"
    )
    consumer_did = owner.json().get("did")
    assert consumer_did, (
        f"controller {controller!r} holds no DID — registered but not onboarded "
        "into the dataspace, which the export reports as a configuration error"
    )

    r = httpx.get(
        f"{CONNECTOR_URL}/consent/admin/shares",
        params={"offer_id": CONSENT_OFFER, "consumer_id": consumer_did},
        headers=auth,
        timeout=10,
    )
    assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
    body = r.json()
    assert isinstance(body.get("datasets"), list) and body["datasets"], (
        "no per-dataset audience in the response; the export reads "
        "`datasets[].subject_ids` and refuses a flattened answer"
    )
    assert "subject_ids" in body["datasets"][0]
