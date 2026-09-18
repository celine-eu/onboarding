"""Half two — the things a schema cannot say.

Two of the three failures this plan repairs were invisible to OpenAPI:

- `GET /admin/owners/{owner_id}` matched on **id**, and this service passed an
  **alias**. The path template and the method were both exactly as published; a
  404 was the only way to find out.
- `POST /consent/admin/shares` refuses a contract-based offer with 409. That is a
  rule about `requires_consent`, not a constraint any schema carries.

A third kind joined them: **who may call a route at all**. ds decides that from
the class of the caller's token, and no schema says so — a consent is registered
by an organisation's own client, and the plain service client this suite
authenticates as is refused. Those checks are written so that a grant quietly
reappearing on the service client fails here rather than passing everywhere.

So these call ds and assert behaviour. They are read-only or deliberately
invalid: nothing here creates a participant, issues a credential or records a
disclosure, because a check that mutates a shared dev stack gets switched off.
"""

from __future__ import annotations

import httpx
import pytest

from .conftest import (
    CLIENT_ID,
    CONNECTOR_URL,
    CONSENT_OFFER,
    CONTRACT_OFFER,
    FIXTURE_IDS,
    IR_URL,
    NO_CONTRACT_OFFER,
    OWNER_ALIAS,
    OWNER_ID,
    PROBE_SUBJECT,
    skip_unconfigured,
)

pytestmark = pytest.mark.ds_contract


@pytest.fixture(autouse=True)
def _ds_is_up(specs):
    """Skip, do not fail, when ds is not there or its fixtures are not named.

    These call ds directly rather than through a fixture, so without this an
    unreachable stack surfaces as a `ConnectError` failure — and a red suite that
    means "nothing was checked" is the noise that gets a check deleted. Depending
    on `specs` reuses the schema half's reachability probe, so both halves skip on
    one signal and say the same thing.

    The second half is this module's own. Every assertion below names seeded
    data — an owner whose id and alias differ, the offer published under each
    legal basis, a subject to probe with — and every deployment seeds its own.
    Unset, they would reach ds as `None` and fail as though ds had changed.
    """
    skip_unconfigured(FIXTURE_IDS, "the seeded data these checks assert against")


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


def test_a_service_token_may_not_register_a_consent(auth):
    """The breaking change this service was rebuilt around, asserted at its source.

    Registering a consent is an act of an *organisation*. `{CLIENT_ID}` is a
    plain service client, bound to no participant, so ds refuses it here — and
    this service now authenticates as the community's own
    `svc-ds-connector-<alias>` instead. If this ever answers anything but 403,
    somebody has put the grant back on a client that could write a consent at any
    connector for anybody's members, and the switch below it would go unnoticed.

    Deliberately invalid besides: the subject does not exist and the offer is
    nonsense, so nothing is created whichever way it goes.
    """
    r = httpx.post(
        f"{CONNECTOR_URL}/consent/admin/shares",
        headers=auth,
        timeout=10,
        json={
            "subject_id": PROBE_SUBJECT,
            "offer_id": "no-such-offer-contract-check",
            "enabled": True,
            "legal_basis": {
                "source": "onboarding-contract-check",
                "consent_text_version": "0",
                "rendered_text_sha256": "0" * 64,
            },
        },
    )
    assert r.status_code == 403, (
        f"expected 403 for the plain service client {CLIENT_ID}, got "
        f"{r.status_code}: {r.text[:200]}. Consent is registered by an "
        f"organisation's own client; a service client writing one is the hole "
        f"ds closed."
    )


def test_the_collectors_read_back_exists_and_is_not_a_service_route(auth):
    """`GET /consent/admin/subject-shares`, and the same refusal.

    It is how a member sees a decision recorded at a holder they have no standing
    at. Same caller class as the write, so the same 403 for a service token — a
    404 here would mean the route is gone and the member's page shows a granted
    release as ungranted.
    """
    r = httpx.get(
        f"{CONNECTOR_URL}/consent/admin/subject-shares",
        params={"subject_id": PROBE_SUBJECT},
        headers=auth,
        timeout=10,
    )
    assert r.status_code != 404, (
        "GET /consent/admin/subject-shares is gone; nothing can read back what a "
        "holder recorded for a member, and the sharing page under-reports it"
    )
    assert r.status_code == 403, (
        f"expected 403 for the plain service client {CLIENT_ID}, got "
        f"{r.status_code}: {r.text[:200]}"
    )


@pytest.mark.needs_contract_offer
def test_a_contract_offer_is_refused_before_anything_else(auth):
    """Disclosed, not consented — the rule Phase 0 enforces at capture.

    Since a service token is refused outright (above), this can no longer see the
    409 from this client. What it still proves is that the refusal is not a 200:
    a contract-based offer never yields a recorded consent, whoever asks.
    """
    r = httpx.post(
        f"{CONNECTOR_URL}/consent/admin/shares",
        headers=auth,
        timeout=10,
        json={
            "subject_id": PROBE_SUBJECT,
            "offer_id": CONTRACT_OFFER,
            "enabled": True,
            "legal_basis": {
                "source": "onboarding-contract-check",
                "consent_text_version": "0",
                "rendered_text_sha256": "0" * 64,
            },
        },
    )
    assert r.status_code in (403, 409), (
        f"expected 403 (service client) or 409 (not consent-based) for "
        f"{CONTRACT_OFFER!r}, got {r.status_code}: {r.text[:200]}. Phase 0 "
        f"rejects these at capture on the strength of this rule."
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
        f"403 from /admin/disclosure — {CLIENT_ID} has lost "
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
    if not NO_CONTRACT_OFFER:
        assert offers[CONTRACT_OFFER]["requires_consent"] is False


def test_an_offers_prerequisites_are_published(auth):
    """`requires_offers` is the connector's, and this service only presents it.

    An offer admitted only together with another is enforced by ds; the member's
    page says so beside the toggle. If the field stops being published, a member
    grants something that admits nobody and nothing on this side can explain why.
    """
    r = httpx.get(f"{CONNECTOR_URL}/ns/sharing-offers", timeout=10)
    assert r.status_code == 200
    offers = {o["id"]: o for o in r.json()}
    assert "requires_offers" in offers[CONSENT_OFFER], (
        "the published projection no longer names an offer's prerequisites; the "
        "sharing page cannot tell a member what their decision waits for"
    )


@pytest.mark.declares_no_contract_offer
def test_a_deployment_declaring_no_contract_offer_publishes_none(auth):
    """The declaration `DS_CONTRACT_CONTRACT_OFFER=none`, checked rather than trusted.

    It deselects the contract-offer checks, so it must not be able to hide an offer
    that exists: if the vocabulary publishes one, the deployment has to name it.
    """
    r = httpx.get(f"{CONNECTOR_URL}/ns/sharing-offers", timeout=10)
    assert r.status_code == 200
    contract = sorted(o["id"] for o in r.json() if o["requires_consent"] is False)
    assert contract == [], (
        f"DS_CONTRACT_CONTRACT_OFFER=none, but the connector publishes {contract} — "
        "name one of them so its checks run"
    )


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
        f"403 from GET /consent/admin/shares — {CLIENT_ID} has lost "
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


@pytest.mark.needs_contract_offer
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
    """Offer to recipient to DID to audience, in the order the export walks it.

    Each hop is published and none of them is inferred: the offer names the party
    the data goes to by alias, the registry maps that alias to the identifier the
    consent plane is keyed by, and the consent plane answers for it. If any hop
    stops working the export cannot name a recipient, and the failure is worth
    seeing here rather than at the moment somebody exports.

    **Either spelling.** ds renamed `recipients.controller` to
    `recipients.recipient`; this service reads the new one and falls back, so
    that an upgrade of the two need not be simultaneous. Asserting only the new
    name here would report drift the code does not have — what matters is that
    *a* recipient is published, and that it is the same one the registry knows.

    It also asserts the shape the export refuses to flatten — one subject set
    per dataset, never merged. A caller reading the first element is correct
    until a second dataset declares the same offer, and then silently wrong.
    """
    offers = httpx.get(f"{CONNECTOR_URL}/ns/sharing-offers", timeout=10).json()
    offer = next(o for o in offers if o["id"] == CONSENT_OFFER)
    recipients = offer.get("recipients") or {}
    controller = recipients.get("recipient") or recipients.get("controller")
    assert controller, (
        f"{CONSENT_OFFER!r} names no recipient under either spelling — the export "
        "has nothing to resolve a recipient from, and must not guess one"
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
