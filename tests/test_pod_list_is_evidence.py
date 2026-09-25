"""The POD list is the collector's own dated evidence, not a disclosure (ADR-0010).

There is no handover (the maintainer, 2026-09-25): the file stays with the community
that collected the decisions, as its record of which supply points stood authorised,
and which were withdrawn, under an offer at the moment it was generated. So the export
records no `DataDisclosed` and posts nothing to `/admin/disclosure` — recording one
would assert a release that never happens — and its header says what the file is.
"""

# The `connectors` fixture is imported from the routing tests and requested by name,
# which ruff reads as a redefinition.
# ruff: noqa: F811

from __future__ import annotations

import re

from test_pod_list_routing import (  # noqa: F401 - the fixture is used by name
    ALICE,
    DSO_CONNECTOR,
    OWN_CONNECTOR,
    RELEASE,
    RESEARCH,
    _export,
    connectors,
)

import celine.onboarding.services.dataspace_identity as di


async def test_no_disclosure_is_posted_anywhere(tmp_path, connectors):
    connectors.holds(OWN_CONNECTOR, RESEARCH, ("rec_meters", {ALICE}))
    connectors.holds(DSO_CONNECTOR, RESEARCH, ("dso_readings", {ALICE}))

    count, _ = await _export(tmp_path, RESEARCH)

    assert count == 1
    assert connectors.disclosed == []
    assert all(method != "POST" for _, method, _ in connectors.requests)


async def test_a_connector_that_would_refuse_a_disclosure_does_not_stop_the_export(
    tmp_path, connectors
):
    """Nothing is asked of it, so nothing it would refuse can stop the evidence —
    which also retires the partial disclosure across holders: recorded at one,
    refused at the next, with no file written."""
    connectors.refuse_disclosures = True
    connectors.holds(DSO_CONNECTOR, RELEASE, ("dso_readings_15m", {ALICE}))

    count, _ = await _export(tmp_path, RELEASE)

    assert count == 1


async def test_the_header_describes_evidence_not_a_release(tmp_path, connectors):
    connectors.holds(DSO_CONNECTOR, RELEASE, ("dso_readings_15m", {ALICE}))

    _, text = await _export(tmp_path, RELEASE)

    # The offer's own id is not the header's wording; the fixture's happens to
    # contain one of the words checked for.
    header = "\n".join(line for line in text.splitlines() if line.startswith("#"))
    header = header.replace(RELEASE, "<offer>")
    assert "# Evidence:" in header
    assert "stood authorised" in header and "at the generation time" in header
    for word in ("release", "disclos", "handover", "hand over", "handed"):
        assert not re.search(word, header, re.IGNORECASE), f"header still says {word!r}"


async def test_the_export_takes_no_recipient_and_asks_the_registry_once(
    tmp_path, connectors, monkeypatch
):
    """The party the consent is read for comes from the offer, resolved once; the
    caller names nobody, so there is no second party to look up and compare."""
    from datetime import UTC, datetime

    from test_pod_list_routing import REC

    from celine.onboarding.outputs import csv_export

    asked: list[str] = []
    resolve = di.check_organization

    async def _counting(name):
        asked.append(name)
        return await resolve(name)

    monkeypatch.setattr(di, "check_organization", _counting)
    connectors.holds(DSO_CONNECTOR, RELEASE, ("dso_readings_15m", {ALICE}))

    count = await csv_export.export_pod_list(
        None,
        tmp_path / "pods.csv",
        rec_slug=REC,
        offer_id=RELEASE,
        generated_at=datetime(2026, 9, 25, 9, 0, tzinfo=UTC),
    )

    assert count == 1
    assert len(asked) == 1
