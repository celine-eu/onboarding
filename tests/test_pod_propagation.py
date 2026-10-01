"""A corrected POD reaches the registry in one write, then the holders' consent keys.

`registry_delivery_point` puts the new POD on the registry member with
`replaces=<the POD the registry holds>`: the registry adds it, removes the old one
and relinks the member's meters in one transaction (R15), and refuses a POD another
active member holds (`delivery_point_held`, R16). `consent_keys` then re-sends the
member's grants at every holder with the keys read from the registry, so the
holder's data plane finds their rows under the new POD (R8); a grant refused
earlier for lack of a POD is granted by the same run.

The connector's own record of the key change (ds ADR-0022, `key_change` events) is
ds's, and is covered by ds `tests/test_consent_holder_keys.py`.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from celine.sdk.rec_registry import RecRegistryApiError
from test_dataspace_shares import (  # noqa: F401 — fixtures, used by name
    HOLDER_URL,
    OWN_OFFER,
    RELEASE,
    _Connectors,
    _consented,
    _enable_shares,
    _mock_token_provider,
    _org_client,
    _patch_httpx,
    _reset_token_provider,
)
from test_propagation import (  # noqa: F401 — fixtures, used by name
    COMMUNITY,
    DID,
    approved,
    correct,
    states,
    trail,
    world,
)
from test_revision import FakeDb

import celine.onboarding.services.dataspace_identity as di
from celine.onboarding.config.settings import settings
from celine.onboarding.services import propagation, rec_registry
from celine.onboarding.services import template_service as _ts
from celine.onboarding.services.revision import PropagationStep, RevisableField

#: Taken before any fixture stubs it: the export reads the real routing.
_REAL_DATASPACE_BINDING = _ts.dataspace_binding

DECLARED = "IT001E00000001"
CORRECT = "IT001E00000002"
THIRD = "IT001E00000003"
OTHER = "IT001E00000009"


@pytest.fixture()
def pod_world(world, monkeypatch):  # noqa: F811 — the fixture, by name
    """The `world` of the name and email tests, with the member's POD registered
    as approval left it, a meter on it, and the key refresh recorded rather than
    sent (the relay itself is tested below, against fake connectors)."""
    submission = approved()
    world.registry.points[submission.ref] = [DECLARED]
    world.registry.meters[submission.ref] = [{"id": "meter-1", "pod": DECLARED}]
    refreshed: list[str] = []

    async def _refresh(sub, *, report):
        refreshed.append(sub.pod_code)
        report.append(f"{RELEASE} granted at example-dso")
        return True

    monkeypatch.setattr(di, "refresh_keys", _refresh)
    monkeypatch.setattr(settings, "ds_connector_url", "http://connector:30001")
    world.submission = submission
    world.refreshed = refreshed
    return world


# ── the registry, in one write ───────────────────────────────────────────────


async def test_the_registry_replaces_the_pod_and_relinks_its_meters_in_one_write(pod_world):
    submission = pod_world.submission

    row = await correct(submission, RevisableField.POD_CODE, CORRECT)

    assert states(row) == {"registry_delivery_point": "done", "consent_keys": "done"}
    # One call: the new point, approval's body, and the one it replaces.
    assert pod_world.registry.puts == [
        (
            COMMUNITY,
            submission.ref,
            CORRECT,
            rec_registry.delivery_point_body(CORRECT),
            DECLARED,
        )
    ]
    assert pod_world.registry.points[submission.ref] == [CORRECT]
    assert pod_world.registry.meters[submission.ref] == [{"id": "meter-1", "pod": CORRECT}]
    # Then the holders, with the POD the submission holds now.
    assert pod_world.refreshed == [CORRECT]
    assert row.steps[1].outcome == "resent:1"
    assert RELEASE in row.steps[1].reason


async def test_a_pod_another_member_holds_is_shown_to_the_operator(pod_world):
    pod_world.registry.put_error = RecRegistryApiError(
        "Delivery point is held by member ex-00001", status_code=409, code="delivery_point_held"
    )

    row = await correct(pod_world.submission, RevisableField.POD_CODE, OTHER)

    step = row.steps[0]
    assert (step.status, step.error_code) == ("failed", "delivery_point_held")
    assert step.reason == (
        "This POD is already recorded for another member — check it with the member."
    )
    # The registry's sentence named the other member; the row does not.
    assert "ex-00001" not in step.reason
    # The holders are not sent a POD the registry refused.
    assert states(row)["consent_keys"] == "pending"
    assert pod_world.refreshed == []


async def test_a_previous_pod_the_registry_no_longer_holds_is_named_without_values(pod_world):
    pod_world.registry.points[pod_world.submission.ref] = [OTHER]

    row = await correct(pod_world.submission, RevisableField.POD_CODE, CORRECT)

    step = row.steps[0]
    assert (step.status, step.error_code) == ("failed", "previous_pod_not_found")
    assert "no longer holds the previous POD" in step.reason
    for value in (DECLARED, CORRECT, OTHER):
        assert value not in step.reason


async def test_a_member_with_no_previous_pod_gets_a_plain_put(pod_world):
    submission = pod_world.submission
    submission.pod_code = None
    pod_world.registry.points[submission.ref] = []

    await correct(submission, RevisableField.POD_CODE, CORRECT)

    assert pod_world.registry.puts[0][4] is None
    assert pod_world.registry.points[submission.ref] == [CORRECT]


async def test_after_a_failed_correction_the_next_replaces_what_the_registry_holds(pod_world):
    submission = pod_world.submission
    pod_world.registry.put_error = RecRegistryApiError("x", status_code=502)
    await correct(submission, RevisableField.POD_CODE, CORRECT)

    pod_world.registry.put_error = None
    second = await correct(submission, RevisableField.POD_CODE, THIRD)

    # The first never reached the registry, so the second replaces the POD
    # approval registered, not the one the first meant to write.
    assert pod_world.registry.puts[-1][2:] == (
        THIRD,
        rec_registry.delivery_point_body(THIRD),
        DECLARED,
    )
    assert states(second) == {"registry_delivery_point": "done", "consent_keys": "done"}
    assert pod_world.registry.points[submission.ref] == [THIRD]


async def test_corrections_in_a_row_each_replace_the_last(pod_world):
    submission = pod_world.submission
    await correct(submission, RevisableField.POD_CODE, CORRECT)
    await correct(submission, RevisableField.POD_CODE, THIRD)

    assert [put[4] for put in pod_world.registry.puts] == [DECLARED, CORRECT]
    assert pod_world.registry.points[submission.ref] == [THIRD]
    assert pod_world.registry.meters[submission.ref][0]["pod"] == THIRD


async def test_a_retry_after_the_registry_was_unavailable(pod_world):
    submission = pod_world.submission
    pod_world.registry.put_error = RecRegistryApiError("x", status_code=503)
    row = await correct(submission, RevisableField.POD_CODE, CORRECT)
    assert states(row) == {"registry_delivery_point": "failed", "consent_keys": "pending"}

    pod_world.registry.put_error = None
    await propagation.run(FakeDb(), submission, row, only=PropagationStep.REGISTRY_DELIVERY_POINT)

    assert states(row) == {"registry_delivery_point": "done", "consent_keys": "done"}
    assert pod_world.refreshed == [CORRECT]


async def test_a_revoked_member_is_not_propagated(pod_world):
    from datetime import UTC, datetime

    from celine.onboarding.models.enablement import EnablementStatus

    pod_world.login["keycloak_user"] = SimpleNamespace(
        status=EnablementStatus.PENDING, completed_at=datetime.now(UTC), external_ref=None
    )

    row = await correct(pod_world.submission, RevisableField.POD_CODE, CORRECT)

    assert set(states(row).values()) == {"skipped"}
    assert pod_world.registry.puts == []


async def test_a_refused_key_refresh_fails_its_step_without_values(pod_world, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    async def _refuse(sub, *, report):
        raise ValueError(f"Share provisioning failed: {RELEASE}: dso 502 keys pod:{sub.pod_code}")

    monkeypatch.setattr(di, "refresh_keys", _refuse)

    row = await correct(pod_world.submission, RevisableField.POD_CODE, CORRECT)

    step = row.steps[1]
    assert (step.status, step.error_code) == ("failed", "consent_refused")
    assert CORRECT not in step.reason


async def test_a_member_outside_the_dataspace_skips_the_keys(pod_world, monkeypatch):
    monkeypatch.setattr(settings, "ds_connector_url", "")

    row = await correct(pod_world.submission, RevisableField.POD_CODE, CORRECT)

    assert states(row) == {"registry_delivery_point": "done", "consent_keys": "skipped"}


# ── the export lists the new POD ─────────────────────────────────────────────


async def test_the_pod_list_export_lists_the_corrected_pod(
    pod_world, tmp_path, monkeypatch, bind_rec
):
    """The export reads the registry, so after the correction it is the new POD
    the distributor gets — through the real `supply_points_by_did`, over the
    registry the correction wrote to."""
    from test_pod_list_export import _export

    from celine.onboarding.outputs import csv_export
    from celine.onboarding.services import template_service

    submission = pod_world.submission
    await correct(submission, RevisableField.POD_CODE, CORRECT)

    import celine.onboarding.services.dataspace_identity as di_

    async def _offer(rec_slug, offer_id):
        from test_pod_list_export import OFFER_RECORD

        return OFFER_RECORD

    async def _audience(offer_id, consumer_id, routes):
        return di_.OfferAudience(
            offer_id=offer_id,
            datasets=(
                di_.DatasetAudience(
                    dataset_id="datasets.silver.meters_15m",
                    route=routes[0],
                    subject_ids=frozenset({DID}),
                    subject_count=1,
                ),
            ),
        )

    async def _decisions(offer_id, routes):
        return di_.OfferDecisions(offer_id=offer_id, cells={})

    async def _consumer(alias):
        return "did:web:grid-operator.example"

    bind_rec("example", organization="example-rec")
    monkeypatch.setattr(template_service, "dataspace_binding", _REAL_DATASPACE_BINDING)
    monkeypatch.setattr(csv_export.settings, "ds_connector_url", "http://connector")
    monkeypatch.setattr(csv_export.settings, "rec_registry_url", "http://registry:8000")
    monkeypatch.setattr(template_service, "get_sharing_offer", _offer)
    monkeypatch.setattr(di_, "get_offer_audience", _audience)
    monkeypatch.setattr(di_, "get_offer_decisions", _decisions)
    monkeypatch.setattr(di_, "resolve_consumer_did", _consumer)

    row = SimpleNamespace(
        ref=submission.ref,
        rec_slug="example",
        # The column holds the corrected POD too; the registry is what is read.
        pod_code=CORRECT,
        dataspace_did=DID,
        dataspace_subject_id=DID,
        data_sharing_consent=True,
        data_sharing_consent_offer_ids=["household-energy-flexibility"],
        data_sharing_consent_text_version="1.0",
        share_provisioned=True,
    )
    count, text = await _export(tmp_path, [row])

    assert count == 1
    assert CORRECT in text
    assert DECLARED not in text


# ── the relay: grants re-sent with the new keys ──────────────────────────────


@pytest.fixture()
def holder(monkeypatch, submission, _enable_shares, bind_rec):  # noqa: F811 — the fixture, by name
    """The member granted the release at the grid operator's connector."""
    bind_rec(
        "default",
        organization="rec-example",
        organization_did="did:web:rec.example",
        linked_participant_did="did:web:rec.example",
        connectors=[{"holder": "example-dso", "url": HOLDER_URL, "offers": [RELEASE]}],
    )
    di._token_provider = _mock_token_provider()
    _consented(submission)
    submission.pod_code = DECLARED
    submission.data_sharing_consent_offer_ids = [OWN_OFFER, RELEASE]
    state = {"pods": [DECLARED]}

    async def _supply_points(dids, *, rec_slug):
        return {submission.dataspace_did: list(state["pods"])}

    monkeypatch.setattr(rec_registry, "supply_points_by_did", _supply_points)
    fake = _Connectors()
    _patch_httpx(monkeypatch, fake.handler)
    fake.state = state
    return fake


def _holder_posts(fake):
    return [body for base, body in fake.posts() if base == HOLDER_URL]


async def test_a_standing_grant_is_re_sent_with_the_new_keys(submission, holder):
    assert await di.provision_user_shares(submission) is True
    assert holder.rows[(HOLDER_URL, RELEASE)]["keys"] == [f"pod:{DECLARED}"]
    holder.state["pods"] = [CORRECT]
    holder.requests.clear()

    # Without the refresh a standing grant is left alone: the gap this closes.
    assert await di.provision_user_shares(submission) is True
    assert _holder_posts(holder) == []

    report: list[str] = []
    assert await di.refresh_keys(submission, report=report) is True

    posts = _holder_posts(holder)
    assert len(posts) == 1
    assert (posts[0]["offer_id"], posts[0]["enabled"], posts[0]["keys"]) == (
        RELEASE,
        True,
        [f"pod:{CORRECT}"],
    )
    assert posts[0]["decided_by"] == "subject"
    assert holder.rows[(HOLDER_URL, RELEASE)]["keys"] == [f"pod:{CORRECT}"]
    # The community's own connector needs no keys and is not written to.
    assert [b for base, b in holder.posts() if base != HOLDER_URL] == []
    assert len(report) == 1 and RELEASE in report[0]
    assert CORRECT not in report[0]


async def test_a_grant_refused_for_lack_of_a_pod_is_granted(submission, holder):
    holder.state["pods"] = []
    assert await di.provision_user_shares(submission) is False
    assert (HOLDER_URL, RELEASE) not in holder.rows

    holder.state["pods"] = [CORRECT]
    report: list[str] = []
    assert await di.refresh_keys(submission, report=report) is True

    assert holder.rows[(HOLDER_URL, RELEASE)]["status"] == "granted"
    assert holder.rows[(HOLDER_URL, RELEASE)]["keys"] == [f"pod:{CORRECT}"]


async def test_a_withdrawn_grant_is_not_re_granted_by_a_refresh(submission, holder):
    assert await di.provision_user_shares(submission) is True
    holder.decide(HOLDER_URL, RELEASE, granted=False)
    holder.state["pods"] = [CORRECT]
    holder.requests.clear()

    await di.refresh_keys(submission, report=[])

    assert holder.rows[(HOLDER_URL, RELEASE)]["status"] == "revoked"
    assert [p for p in _holder_posts(holder) if p["enabled"]] == []


async def test_a_holder_refusing_the_refresh_raises(submission, holder):
    assert await di.provision_user_shares(submission) is True
    holder.state["pods"] = [CORRECT]
    holder.refuse[HOLDER_URL] = 502

    with pytest.raises(ValueError, match="Share provisioning failed"):
        await di.refresh_keys(submission, report=[])
