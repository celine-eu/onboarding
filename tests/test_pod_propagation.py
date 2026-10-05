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


# ── the ordinary retry: stale keys are refreshed, equal keys are left alone ──
#
# A POD changed in the registry outside onboarding's revisions reaches the holder
# on the next retry of the enablement step: `provision_user_shares` compares the
# keys the holder returns for the grant this community registered with
# `subject_supply_keys`, as sets.


async def test_a_retry_re_sends_a_standing_grant_whose_keys_changed(submission, holder):
    assert await di.provision_user_shares(submission) is True
    holder.state["pods"] = [CORRECT]
    holder.requests.clear()

    report: list[str] = []
    assert await di.provision_user_shares(submission, report=report) is True

    posts = _holder_posts(holder)
    assert [(p["offer_id"], p["enabled"], p["keys"], p["decided_by"]) for p in posts] == [
        (RELEASE, True, [f"pod:{CORRECT}"], "subject")
    ]
    assert holder.rows[(HOLDER_URL, RELEASE)]["keys"] == [f"pod:{CORRECT}"]
    # Never keys, nor any write, at the community's own connector.
    assert [b for base, b in holder.posts() if base != HOLDER_URL] == []
    assert len(report) == 1 and RELEASE in report[0] and CORRECT not in report[0]

    # Refreshed once: the next retry finds the keys equal and writes nothing.
    holder.requests.clear()
    assert await di.provision_user_shares(submission) is True
    assert holder.posts() == []


async def test_a_retry_leaves_a_standing_grant_with_the_same_keys_alone(submission, holder):
    holder.state["pods"] = [DECLARED, CORRECT]
    assert await di.provision_user_shares(submission) is True
    # The same supply points, in another order: a set, not a list.
    holder.state["pods"] = [CORRECT, DECLARED]
    holder.requests.clear()

    assert await di.provision_user_shares(submission) is True
    assert holder.posts() == []


async def test_a_retry_never_re_sends_a_withdrawal_for_changed_keys(submission, holder):
    assert await di.provision_user_shares(submission) is True
    holder.decide(HOLDER_URL, RELEASE, granted=False)
    holder.state["pods"] = [CORRECT]
    holder.requests.clear()

    await di.provision_user_shares(submission)

    assert holder.rows[(HOLDER_URL, RELEASE)]["status"] == "revoked"
    assert _holder_posts(holder) == []


async def test_a_retry_does_not_replace_keys_while_the_registry_is_unreadable(
    submission, holder, monkeypatch
):
    holder.state["pods"] = [CORRECT]
    assert await di.provision_user_shares(submission) is True

    async def _down(dids, *, rec_slug):
        raise RecRegistryApiError("x", status_code=503)

    # The declared POD stands in for the registry only to grant; it predates the
    # correction the holder already holds, so it never replaces it.
    monkeypatch.setattr(rec_registry, "supply_points_by_did", _down)
    holder.requests.clear()

    assert await di.provision_user_shares(submission) is True
    assert holder.posts() == []
    assert holder.rows[(HOLDER_URL, RELEASE)]["keys"] == [f"pod:{CORRECT}"]


async def test_a_retry_leaves_a_grant_whose_keys_it_cannot_read(submission, holder):
    # A grant the holder returns no keys for — another party registered them
    # (ds `_keys_for`) — has nothing to compare and is not overwritten.
    assert await di.provision_user_shares(submission) is True
    holder.rows[(HOLDER_URL, RELEASE)]["keys"] = []
    holder.rows[(HOLDER_URL, RELEASE)]["collector"] = "did:web:another-collector.example"
    holder.state["pods"] = [CORRECT]
    holder.requests.clear()

    assert await di.provision_user_shares(submission) is True
    assert holder.posts() == []


# ── R19: no POD left, the grant waits ─────────────────────────────────────────


async def test_no_pod_left_keeps_the_grant_and_empties_its_keys(submission, holder):
    """R19. The registry, asked, holds no POD for the member any more: the
    standing grant is re-sent with `keys: []` — not withdrawn, not left with the
    old POD — so the holder's `subject_key_match` releases nothing for it."""
    assert await di.provision_user_shares(submission) is True
    holder.state["pods"] = []
    holder.requests.clear()
    report: list[str] = []

    assert await di.provision_user_shares(submission, raise_on_error=True, report=report) is True

    posts = [b for base, b in holder.posts() if base == HOLDER_URL]
    assert len(posts) == 1
    sent = posts[0]
    assert (sent["offer_id"], sent["enabled"], sent["decided_by"]) == (RELEASE, True, "subject")
    # Sent, and empty: absent would leave the old POD in force at the holder.
    assert "keys" in sent and sent["keys"] == []
    row = holder.rows[(HOLDER_URL, RELEASE)]
    assert (row["status"], row["keys"]) == ("granted", [])
    assert "released for none" in report[0]
    # Nothing withdrawn anywhere, and the community's own connector untouched.
    assert [b for base, b in holder.posts() if base != HOLDER_URL] == []


async def test_a_keyless_grant_is_not_re_sent_again_and_again(submission, holder):
    assert await di.provision_user_shares(submission) is True
    holder.state["pods"] = []
    assert await di.provision_user_shares(submission) is True
    holder.requests.clear()

    assert await di.provision_user_shares(submission) is True

    assert holder.posts() == []


async def test_a_pod_added_later_re_sends_the_keys(submission, holder):
    """After R19 emptied the keys, a POD added in the registry is carried on the
    next ordinary run; one added by revision, by the forced refresh."""
    assert await di.provision_user_shares(submission) is True
    holder.state["pods"] = []
    assert await di.provision_user_shares(submission) is True
    holder.state["pods"] = [CORRECT]
    holder.requests.clear()

    assert await di.provision_user_shares(submission) is True

    posts = [b for base, b in holder.posts() if base == HOLDER_URL]
    assert [p["keys"] for p in posts] == [[f"pod:{CORRECT}"]]
    assert holder.rows[(HOLDER_URL, RELEASE)]["keys"] == [f"pod:{CORRECT}"]


async def test_the_forced_refresh_also_empties_and_refills(submission, holder):
    assert await di.provision_user_shares(submission) is True
    holder.state["pods"] = []
    assert await di.refresh_keys(submission, report=[]) is True
    assert holder.rows[(HOLDER_URL, RELEASE)]["keys"] == []

    holder.state["pods"] = [CORRECT]
    assert await di.refresh_keys(submission, report=[]) is True
    assert holder.rows[(HOLDER_URL, RELEASE)]["keys"] == [f"pod:{CORRECT}"]


async def test_a_new_grant_without_a_pod_is_still_refused(submission, holder):
    holder.state["pods"] = []

    with pytest.raises(ValueError, match="no supply point is recorded"):
        await di.provision_user_shares(submission, raise_on_error=True)

    assert [b for base, b in holder.posts() if base == HOLDER_URL] == []
    assert (HOLDER_URL, RELEASE) not in holder.rows


async def test_an_unreadable_registry_never_empties_the_keys(submission, holder, monkeypatch):
    """Only the registry's own answer may empty a holder's keys: with the
    registry down the standing grant is refused rather than emptied (and the
    declared POD never stands in for it, REQ-0046)."""
    assert await di.provision_user_shares(submission) is True
    submission.pod_code = None

    async def _down(dids, *, rec_slug):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(rec_registry, "supply_points_by_did", _down)
    holder.requests.clear()

    with pytest.raises(ValueError, match="registry could not be read"):
        await di.refresh_keys(submission, report=[])

    assert holder.posts() == []
    assert holder.rows[(HOLDER_URL, RELEASE)]["keys"] == [f"pod:{DECLARED}"]


async def test_a_withdrawal_is_untouched_by_r19(submission, holder):
    assert await di.provision_user_shares(submission) is True
    holder.decide(HOLDER_URL, RELEASE, granted=False)
    holder.state["pods"] = []
    holder.requests.clear()

    await di.provision_user_shares(submission)

    assert holder.rows[(HOLDER_URL, RELEASE)]["status"] == "revoked"
    assert [
        p for p in [b for base, b in holder.posts() if base == HOLDER_URL] if p["enabled"]
    ] == []
