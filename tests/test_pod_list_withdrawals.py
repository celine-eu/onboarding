"""The POD list reports who withdrew, as withdrawn — never as absent, never as authorised.

Plan D6, ADR-0009. The export is the collector's own dated evidence, and the audience
read (`GET /consent/admin/shares`) lists standing grants only, so a member who withdrew
was indistinguishable from one nobody asked. ds now lists an organisation's own
members' decisions per offer (`GET /consent/admin/decisions`, ds ADR-0021), and the
export reads it at every connector holding the offer — as the community, with its own
organisation client, following the cursor to the end.

The file keeps the two apart **by column**, not by a flag on a row: a supply point
that stands authorised is in `authorised_pod_code`, one whose member withdrew is in
`withdrawn_pod_code`, with when and by whom. A reader who takes one column cannot
pick up the other's rows, and a reader of the old single `pod_code` column fails
loudly instead of silently reading withdrawals as authorisations.
"""

# The `connectors` fixture is imported from the routing tests and requested by name,
# which ruff reads as a redefinition.
# ruff: noqa: F811

from __future__ import annotations

import csv
import io

import pytest
from test_pod_list_routing import (  # noqa: F401 - the fixture is used by name
    ALICE,
    BOB,
    CAROL,
    COMMUNITY,
    DSO,
    DSO_CONNECTOR,
    INCENTIVE,
    OWN_CONNECTOR,
    PODS,
    RELEASE,
    RESEARCH,
    _export,
    connectors,
)

COLUMNS = ["authorised_pod_code", "withdrawn_pod_code", "withdrawn_at", "withdrawn_by"]
WITHDRAWN_AT = "2026-09-20T08:30:00Z"


def _rows(text: str) -> list[dict]:
    body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    return list(csv.DictReader(io.StringIO(body)))


def _authorised(text: str) -> list[str]:
    return [r["authorised_pod_code"] for r in _rows(text) if r["authorised_pod_code"]]


def _withdrawn(text: str) -> list[dict]:
    return [r for r in _rows(text) if r["withdrawn_pod_code"]]


async def test_a_withdrawal_is_reported_as_withdrawn(tmp_path, connectors):
    """Absent is what the audience says; withdrawn is what the member did."""
    connectors.holds(OWN_CONNECTOR, INCENTIVE, ("settlement_15m", {ALICE}))
    connectors.decides(OWN_CONNECTOR, INCENTIVE, ALICE, "settlement_15m", "granted")
    connectors.decides(
        OWN_CONNECTOR, INCENTIVE, BOB, "settlement_15m", "withdrawn", revoked_at=WITHDRAWN_AT
    )

    count, text = await _export(tmp_path, INCENTIVE)

    assert count == 1
    assert _rows(text)[0].keys() == set(COLUMNS)
    assert _authorised(text) == PODS[ALICE]
    assert _withdrawn(text) == [
        {
            "authorised_pod_code": "",
            "withdrawn_pod_code": PODS[BOB][0],
            "withdrawn_at": WITHDRAWN_AT,
            "withdrawn_by": "subject",
        }
    ]
    assert "NOT authorised" in text
    assert "IT-must-not-be-read" not in text, "the decision's keys are never read into the file"


async def test_the_decisions_are_read_as_the_community_to_the_last_page(tmp_path, connectors):
    """A page may be short and not last: only a null cursor ends the list. And the
    route is the organisation's, not the service client's the audience read uses."""
    connectors.page_size = 1
    connectors.holds(OWN_CONNECTOR, INCENTIVE, ("settlement_15m", set()))
    for subject in (ALICE, BOB, CAROL):
        connectors.decides(
            OWN_CONNECTOR,
            INCENTIVE,
            subject,
            "settlement_15m",
            "withdrawn",
            revoked_at=WITHDRAWN_AT,
        )

    count, text = await _export(tmp_path, INCENTIVE)

    assert count == 0
    assert sorted(r["withdrawn_pod_code"] for r in _withdrawn(text)) == sorted(
        PODS[ALICE] + PODS[BOB] + PODS[CAROL]
    )
    assert len(connectors.decisions_auth) == 3
    assert set(connectors.decisions_auth) == {"Bearer org-token"}


async def test_withdrawals_are_read_at_every_holder(tmp_path, connectors):
    connectors.holds(OWN_CONNECTOR, RESEARCH, ("rec_meters", {ALICE}))
    connectors.holds(DSO_CONNECTOR, RESEARCH, ("dso_readings", {ALICE}))
    for where, dataset in ((OWN_CONNECTOR, "rec_meters"), (DSO_CONNECTOR, "dso_readings")):
        connectors.decides(where, RESEARCH, ALICE, dataset, "granted")
        connectors.decides(
            where,
            RESEARCH,
            BOB,
            dataset,
            "withdrawn",
            revoked_at=WITHDRAWN_AT,
            decided_by="collector",
        )

    _, text = await _export(tmp_path, RESEARCH)

    assert _authorised(text) == PODS[ALICE]
    assert [(r["withdrawn_pod_code"], r["withdrawn_by"]) for r in _withdrawn(text)] == [
        (PODS[BOB][0], "collector")
    ]


# ── the split, and a contradiction, are refused ──────────────────


async def test_granted_in_one_dataset_and_withdrawn_in_another_is_refused(tmp_path, connectors):
    """The D4 split, seen in the decisions: the offer's statement is not true of
    both datasets for this member, so no one list is its audience."""
    connectors.holds(OWN_CONNECTOR, INCENTIVE, ("settlement_15m", {ALICE}), ("commitment", {ALICE}))
    connectors.decides(OWN_CONNECTOR, INCENTIVE, BOB, "settlement_15m", "granted")
    connectors.decides(
        OWN_CONNECTOR, INCENTIVE, BOB, "commitment", "withdrawn", revoked_at=WITHDRAWN_AT
    )

    with pytest.raises(ValueError, match="granted in some datasets and withdrawn in others") as e:
        await _export(tmp_path, INCENTIVE)

    assert "commitment" in str(e.value) and BOB not in str(e.value)
    assert connectors.disclosed == []
    assert not (tmp_path / "pods.csv").exists()


async def test_granted_at_one_holder_and_withdrawn_at_another_is_refused(tmp_path, connectors):
    connectors.holds(OWN_CONNECTOR, RESEARCH, ("rec_meters", {ALICE}))
    connectors.holds(DSO_CONNECTOR, RESEARCH, ("dso_readings", {ALICE}))
    connectors.decides(OWN_CONNECTOR, RESEARCH, CAROL, "rec_meters", "granted")
    connectors.decides(
        DSO_CONNECTOR, RESEARCH, CAROL, "dso_readings", "withdrawn", revoked_at=WITHDRAWN_AT
    )

    with pytest.raises(ValueError, match="granted in some datasets and withdrawn in others") as e:
        await _export(tmp_path, RESEARCH)

    assert f"dso_readings (held by {DSO})" in str(e.value)
    assert connectors.disclosed == []


async def test_authorised_by_the_audience_and_withdrawn_in_the_decisions_is_refused(
    tmp_path, connectors
):
    """Two reads of one connector that disagree about one member. Listing them in
    either column would state one of the two as fact; the export refuses instead."""
    connectors.holds(OWN_CONNECTOR, INCENTIVE, ("settlement_15m", {ALICE}))
    connectors.decides(
        OWN_CONNECTOR, INCENTIVE, ALICE, "settlement_15m", "withdrawn", revoked_at=WITHDRAWN_AT
    )

    with pytest.raises(ValueError, match="1 subject is authorised") as e:
        await _export(tmp_path, INCENTIVE)

    assert ALICE not in str(e.value)
    assert connectors.disclosed == []


# ── when the decisions cannot be read ─────────────────────────────


@pytest.mark.parametrize("status", [404, 405])
async def test_a_connector_without_the_decisions_route_is_marked_not_reported(
    tmp_path, connectors, status
):
    """An older connector serves no decisions route. The authorised list is still
    true evidence (D1/D4 stand on the granted audience alone), so it is written —
    and the header names the holder whose withdrawals it could not report."""
    connectors.holds(DSO_CONNECTOR, RELEASE, ("dso_readings_15m", {ALICE}))
    connectors.lacks_decisions(DSO_CONNECTOR, RELEASE, status)

    count, text = await _export(tmp_path, RELEASE)

    assert count == 1
    assert _withdrawn(text) == []
    assert f"# Withdrawals NOT reported for {DSO}'s connector" in text


@pytest.mark.parametrize("status", [401, 403, 409, 500, 503])
async def test_a_decisions_read_that_fails_refuses_the_export(tmp_path, connectors, status):
    """The route is there and did not answer: who withdrew is unknown, which is not
    the same as nobody withdrawing. No file."""
    connectors.holds(DSO_CONNECTOR, RELEASE, ("dso_readings_15m", {ALICE}))
    connectors.lacks_decisions(DSO_CONNECTOR, RELEASE, status)

    with pytest.raises((RuntimeError, ValueError)):
        await _export(tmp_path, RELEASE)

    assert connectors.disclosed == []
    assert not (tmp_path / "pods.csv").exists()


async def test_the_community_named_in_withdrawn_by_is_its_own_code(tmp_path, connectors):
    """`withdrawn_by` carries ds's code for whose act it was, not a party name."""
    connectors.holds(OWN_CONNECTOR, INCENTIVE, ("settlement_15m", set()))
    connectors.decides(
        OWN_CONNECTOR,
        INCENTIVE,
        BOB,
        "settlement_15m",
        "withdrawn",
        revoked_at=WITHDRAWN_AT,
        decided_by="operator",
    )

    _, text = await _export(tmp_path, INCENTIVE)

    assert _withdrawn(text)[0]["withdrawn_by"] == "operator"
    assert COMMUNITY not in _withdrawn(text)[0].values()
