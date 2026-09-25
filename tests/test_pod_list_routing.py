"""The POD list is read from every connector holding the offer, and exported where they agree.

ADR-0008. Two assumptions the export used to make, each older than the model it
now runs under:

- **one connector per community** — it asked ``DS_CONNECTOR_URL`` whatever the
  offer, so an offer whose data sits entirely at another participant's connector
  was answered ``422`` by the community's own, and the export read that as the
  caller naming the wrong offer;
- **one dataset per offer** — it refused every offer resolving to more than one,
  including the ordinary case where every dataset's audience is the same set of
  people and one file is exactly honest.

What replaces them: the routes come from the REC's ``dataspace.connectors``
(:func:`dataspace_identity.consent_routes`, the same call every consent write
makes), the audience is **checked per dataset** across every route, and the file
is **scoped per offer** — one file when there is one distinct subject set, and a
refusal naming the split when there is more than one.

Only the HTTP edge is stubbed here. The routing, the audience read, the
agreement check and the file are the real functions, so these fail on the
behaviour — the connector asked, what the file says — and not on a signature.
Since ADR-0010 nothing is posted to any connector; the stand-in still accepts a
disclosure so that a test can assert none arrives.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
import pytest
from test_dataspace_identity import _mock_token_provider, _patch_httpx

import celine.onboarding.services.dataspace_identity as di
from celine.onboarding.outputs import csv_export
from celine.onboarding.services import rec_registry, template_service

GENERATED_AT = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)

REC = "example"
COMMUNITY = "example-rec"
DSO = "example-dso"
OWN_CONNECTOR = "http://rec-connector.example.org"
DSO_CONNECTOR = "http://dso-connector.example.org"
RECIPIENT = "example-recipient"
RECIPIENT_DID = "did:web:recipient.example.org"

#: Held only at the grid operator's connector: the community's own holds nothing for it.
RELEASE = "meter-release"
#: Held at both connectors.
RESEARCH = "research"
#: Named by no entry: the community's own connector alone, as before routing existed.
INCENTIVE = "incentive"

ALICE = "did:web:users.example.org:alice"
BOB = "did:web:users.example.org:bob"
CAROL = "did:web:users.example.org:carol"

PODS = {ALICE: ["IT001E00000001"], BOB: ["IT001E00000002"], CAROL: ["IT001E00000003"]}


def _offer(offer_id: str) -> dict:
    return {
        "id": offer_id,
        "purpose": "Research",
        "requires_consent": True,
        "recipients": {"recipient": RECIPIENT, "recipient_role": "operations"},
        "consent_text_version": "1.0",
    }


class _Connectors:
    """Two connectors behind one mock transport, answering by host.

    ``audiences[(host, offer)]`` is the connector's ``datasets`` list, or an int
    for a status it refuses with. Anything not configured answers ``422`` —
    which is what a connector holding no dataset for an offer says. Every request
    is recorded, so a test can say which connector was asked, and that no
    disclosure was posted.
    """

    def __init__(self) -> None:
        self.audiences: dict[tuple[str, str], list[dict] | int] = {}
        self.requests: list[tuple[str, str, str]] = []
        self.disclosed: list[tuple[str, dict]] = []
        #: `GET /consent/admin/decisions`: (host, offer) → {subject: [decision]}, or
        #: an int status. Unset: the datasets the connector holds, and no subject.
        self.decisions: dict[tuple[str, str], dict[str, list[dict]] | int] = {}
        #: The token each decisions read presented — it must be the organisation's.
        self.decisions_auth: list[str] = []
        self.page_size = 1
        #: A connector that would refuse a disclosure, were one ever posted.
        self.refuse_disclosures = False

    def decides(
        self,
        connector: str,
        offer: str,
        subject: str,
        dataset: str,
        state: str,
        *,
        decided_by: str = "subject",
        revoked_at: str | None = None,
    ) -> None:
        cell = {
            "dataset_id": dataset,
            "consent_id": f"c-{subject}-{dataset}",
            "state": state,
            "decided_by": decided_by,
            "collector": "did:web:rec.example.org",
            "decided_at": "2026-09-01T10:00:00Z",
            "revoked_at": revoked_at,
            "keys": ["IT-must-not-be-read"],
        }
        table = self.decisions.setdefault((urlsplit(connector).netloc, offer), {})
        table.setdefault(subject, []).append(cell)

    def lacks_decisions(self, connector: str, offer: str, status: int) -> None:
        self.decisions[(urlsplit(connector).netloc, offer)] = status

    def holds(self, connector: str, offer: str, *datasets: tuple[str, set[str]]) -> None:
        self.audiences[(urlsplit(connector).netloc, offer)] = [
            {"dataset_id": dataset_id, "subject_ids": sorted(ids), "subject_count": len(ids)}
            for dataset_id, ids in datasets
        ]

    def refuses(self, connector: str, offer: str, status: int) -> None:
        self.audiences[(urlsplit(connector).netloc, offer)] = status

    def asked(self, connector: str) -> list[str]:
        host = urlsplit(connector).netloc
        return [path for (h, method, path) in self.requests if h == host]

    def handler(self, req: httpx.Request) -> httpx.Response:
        host = req.url.netloc.decode()
        self.requests.append((host, req.method, req.url.path))
        if req.method == "GET" and req.url.path == "/consent/admin/shares":
            offer = req.url.params["offer_id"]
            answer = self.audiences.get((host, offer), 422)
            if isinstance(answer, int):
                return httpx.Response(answer, text=f"offer {offer!r} resolves to no dataset")
            return httpx.Response(
                200, json={"offer_id": offer, "consumer_id": "x", "datasets": answer}
            )
        if req.method == "GET" and req.url.path == "/consent/admin/decisions":
            self.decisions_auth.append(req.headers.get("authorization", ""))
            offer = req.url.params["offer_id"]
            held = self.audiences.get((host, offer))
            table = self.decisions.get((host, offer), {})
            if isinstance(table, int):
                return httpx.Response(table, text="no such route")
            if not isinstance(held, list):
                return httpx.Response(422, text=f"offer {offer!r} resolves to no dataset")
            # Paged by subject id with an opaque cursor; the stand-in pages one
            # subject at a time, so following the cursor is not optional.
            ordered = sorted(table)
            start = int(req.url.params.get("cursor") or 0)
            page = ordered[start : start + self.page_size]
            more = start + self.page_size < len(ordered)
            return httpx.Response(
                200,
                json={
                    "offer_id": offer,
                    "datasets": [d["dataset_id"] for d in held],
                    "limit": int(req.url.params.get("limit") or 50),
                    "subjects": [{"subject_id": s, "decisions": table[s]} for s in page],
                    "next_cursor": str(start + self.page_size) if more else None,
                },
            )
        if req.method == "POST" and req.url.path == "/admin/disclosure":
            body = json.loads(req.content)
            self.disclosed.append((host, body))
            if self.refuse_disclosures:
                return httpx.Response(403, text="not an accepted collector")
            held = self.audiences.get((host, body["offer_id"]))
            if not isinstance(held, list):
                return httpx.Response(422, text="resolves to no dataset")
            return httpx.Response(
                200,
                json={
                    "status": "recorded",
                    "offer_id": body["offer_id"],
                    "disclosures": [
                        {
                            "dataset_id": d["dataset_id"],
                            "consent_snapshot_hash": "a" * 64,
                            "granted_party_count": d["subject_count"],
                        }
                        for d in held
                    ],
                },
            )
        return httpx.Response(404)


@pytest.fixture()
def connectors(monkeypatch, bind_rec) -> _Connectors:
    bind_rec(
        REC,
        organization=COMMUNITY,
        organization_did="did:web:rec.example.org",
        connectors=[
            {"holder": DSO, "url": DSO_CONNECTOR, "offers": [RELEASE, RESEARCH]},
            {"holder": COMMUNITY, "offers": [RESEARCH]},
        ],
    )
    monkeypatch.setattr(di.settings, "ds_connector_url", OWN_CONNECTOR)
    monkeypatch.setattr(csv_export.settings, "ds_connector_url", OWN_CONNECTOR)
    di._token_provider = _mock_token_provider()

    async def _get_offer(rec_slug, offer_id):
        return _offer(offer_id)

    async def _check(name):
        return di.OwnerCheck(found=True, status="verified", did=RECIPIENT_DID, id=RECIPIENT)

    async def _supply_points(dids, *, rec_slug):
        return {did: PODS[did] for did in dids if did in PODS}

    monkeypatch.setattr(template_service, "get_sharing_offer", _get_offer)
    monkeypatch.setattr(di, "check_organization", _check)
    monkeypatch.setattr(rec_registry, "supply_points_by_did", _supply_points)

    async def _org_headers(alias):
        assert alias == COMMUNITY, "the decisions are read as the community that collected them"
        return {"Authorization": "Bearer org-token"}

    monkeypatch.setattr(di, "organisation_auth_headers", _org_headers)

    fake = _Connectors()
    _patch_httpx(monkeypatch, fake.handler)
    yield fake
    di._token_provider = None


async def _export(tmp_path, offer_id):
    out = tmp_path / "pods.csv"
    count = await csv_export.export_pod_list(
        None,
        out,
        rec_slug=REC,
        offer_id=offer_id,
        generated_at=GENERATED_AT,
    )
    return count, out.read_text(encoding="utf-8")


# ── the route comes from the binding ─────────────────────────────


async def test_an_offer_held_only_by_another_participant_is_read_at_its_holder(
    tmp_path, connectors
):
    """The measured failure: the community's connector holds nothing for the
    release offer and answers 422, and the export reported the offer as wrong.
    The offer was right; the connector asked was not."""
    connectors.holds(DSO_CONNECTOR, RELEASE, ("meters_15m", {ALICE}))

    count, text = await _export(tmp_path, RELEASE)

    assert count == 1
    assert "IT001E00000001" in text
    assert connectors.asked(OWN_CONNECTOR) == [], (
        "the community's connector holds nothing for this offer and must not be asked"
    )


async def test_a_multi_dataset_offer_whose_audiences_agree_is_one_file(tmp_path, connectors):
    """Several datasets, one distinct subject set: one file, exactly honest.

    The header names the datasets and the holder it was computed from, so a
    reader can tell the audience of the whole offer from one dataset's.
    """
    connectors.holds(
        OWN_CONNECTOR,
        INCENTIVE,
        ("settlement_15m", {ALICE, BOB}),
        ("settlement_1h", {ALICE, BOB}),
        ("measurements_15m", {ALICE, BOB}),
    )

    count, text = await _export(tmp_path, INCENTIVE)

    assert count == 2
    assert "IT001E00000001" in text and "IT001E00000002" in text
    for dataset in ("settlement_15m", "settlement_1h", "measurements_15m"):
        assert f"#   {dataset}, held by {COMMUNITY}" in text
    assert connectors.disclosed == []


async def test_disagreeing_audiences_are_refused_naming_the_split(tmp_path, connectors):
    """More than one distinct set: the offer's statement is no longer true of all
    its datasets. Refused — never unioned, never intersected — and the refusal
    says which datasets split and by how much. Nothing is recorded or written."""
    connectors.holds(
        OWN_CONNECTOR,
        INCENTIVE,
        ("settlement_15m", {ALICE, BOB}),
        ("settlement_1h", {ALICE, BOB}),
        ("commitment", {ALICE}),
    )

    with pytest.raises(ValueError) as refused:
        await _export(tmp_path, INCENTIVE)

    message = str(refused.value)
    assert "do not agree" in message
    assert "2 distinct audiences across 3 datasets" in message
    assert "2 subjects: settlement_15m, settlement_1h" in message
    assert "1 subject: commitment" in message
    assert "1 subject is in some of these audiences and not in others" in message
    # The subjects themselves are not named: a refusal is read by an operator
    # and logged, and it has no need of who.
    assert BOB not in message
    assert connectors.disclosed == []
    assert not (tmp_path / "pods.csv").exists()


# ── an offer held at several connectors ──────────────────────────


async def test_an_offer_held_at_two_connectors_is_read_at_both(tmp_path, connectors):
    """ADR-0007's case, on the read side: every holder is asked, the audiences
    are compared across them, and the file names every dataset and holder."""
    connectors.holds(OWN_CONNECTOR, RESEARCH, ("rec_meters", {ALICE, CAROL}))
    connectors.holds(DSO_CONNECTOR, RESEARCH, ("dso_readings", {ALICE, CAROL}))

    count, text = await _export(tmp_path, RESEARCH)

    assert count == 2
    assert connectors.asked(OWN_CONNECTOR) and connectors.asked(DSO_CONNECTOR)
    assert f"#   rec_meters, held by {COMMUNITY}" in text
    assert f"#   dso_readings, held by {DSO}" in text
    assert connectors.disclosed == []


async def test_two_holders_that_disagree_are_refused(tmp_path, connectors):
    """A member withdrawn at one holder and not the other is a split like any
    other — and naming the holder is what lets an operator find it."""
    connectors.holds(OWN_CONNECTOR, RESEARCH, ("rec_meters", {ALICE, CAROL}))
    connectors.holds(DSO_CONNECTOR, RESEARCH, ("dso_readings", {ALICE}))

    with pytest.raises(ValueError, match="do not agree") as refused:
        await _export(tmp_path, RESEARCH)

    assert f"dso_readings (held by {DSO})" in str(refused.value)
    assert connectors.disclosed == []


async def test_a_route_holding_nothing_for_the_offer_contributes_nothing(tmp_path, connectors):
    """A 422 from one route is that connector saying it holds no dataset for the
    offer — expected, and not a refusal of the export. The audience comes from,
    and the header names, the holding connector alone."""
    connectors.holds(DSO_CONNECTOR, RESEARCH, ("dso_readings", {ALICE}))
    connectors.refuses(OWN_CONNECTOR, RESEARCH, 422)

    count, text = await _export(tmp_path, RESEARCH)

    assert count == 1
    assert f"#   dso_readings, held by {DSO}" in text
    assert f"held by {COMMUNITY}" not in text
    assert connectors.disclosed == []


async def test_no_route_holding_the_offer_is_refused_without_blaming_the_offer(
    tmp_path, connectors
):
    """Every routed connector holds nothing: nothing to export, and the message
    points at the routing — not at the caller for naming the offer."""
    connectors.refuses(DSO_CONNECTOR, RELEASE, 422)

    with pytest.raises(ValueError) as refused:
        await _export(tmp_path, RELEASE)

    message = str(refused.value)
    assert "holds a dataset" in message
    assert f"{DSO}'s connector" in message
    assert "dataspace.connectors" in message
    assert connectors.disclosed == []
