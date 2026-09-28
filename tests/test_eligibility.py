from __future__ import annotations

import httpx
import pytest

from celine.onboarding.services import eligibility, template_service
from celine.onboarding.services.eligibility import (
    AddressInfo,
    GeocoderUnavailableError,
    NoRestrictionChecker,
    RulesChecker,
)

TOWN = AddressInfo(
    lat=0.05,
    lng=0.2,
    display_name="Example Town, Example Valley Community, EX",
    municipality="Example Town",
    # Both names a geocoder can return for one point: the town is the `city`
    # key and the `municipality` key is the valley community above it, which is
    # why a rule is matched against every candidate rather than one chosen field.
    municipality_candidates=("Example Town", "Example Valley Community"),
    postal_code="00000",
    state="Example Region",
    country_code="IT",
)


def test_a_rule_matches_a_candidate_that_is_not_the_display_name():
    checker = RulesChecker([{"type": "municipality", "values": ["Example Valley Community"]}])
    result = checker.check(TOWN)
    assert result.eligible
    assert result.matched_value == "Example Valley Community"


def test_outside_the_coverage_area_is_refused_and_says_where():
    checker = RulesChecker([{"type": "municipality", "values": ["Other Town"]}])
    result = checker.check(TOWN)
    assert not result.eligible
    assert "Example Town" in result.reason


def test_a_checker_never_fetches_an_address():
    """The whole point of the shape: rule matching does no network I/O.

    It used to reverse-geocode inside `check`, which cost one call per checker
    and a second one on the address path, where the caller had already resolved
    the address and thrown it away.
    """
    checker = RulesChecker([{"type": "municipality", "values": ["Example Town"]}])
    with pytest.raises(ValueError, match="needs an address"):
        checker.check(None)


def test_no_coverage_rules_needs_no_address():
    checker = NoRestrictionChecker()
    assert checker.requires_address is False
    assert checker.check(None).eligible


@pytest.mark.asyncio
async def test_a_geocoder_timeout_is_not_an_ineligible_applicant(monkeypatch):
    """A read timeout reached the endpoint as a bare 500 and blocked the worker.

    Reported as its own failure so the caller can answer "the address service is
    unavailable" rather than "you are outside the area" — a coverage verdict
    nobody computed is not a refusal.
    """

    class _Timeout:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *a, **kw):
            raise httpx.ReadTimeout("the read operation timed out")

    monkeypatch.setattr(eligibility.httpx, "AsyncClient", lambda **kw: _Timeout())

    with pytest.raises(GeocoderUnavailableError):
        await eligibility.reverse_geocode(0.05, 0.2)
    with pytest.raises(GeocoderUnavailableError):
        await eligibility.geocode_address("Example Town")


@pytest.mark.asyncio
async def test_the_sweep_resolves_the_address_once_for_every_community(monkeypatch):
    calls = []

    async def _reverse(lat, lng):
        calls.append((lat, lng))
        return TOWN

    monkeypatch.setattr(eligibility, "reverse_geocode", _reverse)
    monkeypatch.setattr(template_service, "get_slugs", lambda: ["a", "b", "c"])
    monkeypatch.setattr(
        template_service,
        "load_manifest",
        lambda slug: {
            "name": slug,
            "coverage": {"type": "municipalities", "municipalities": ["Example Town"]},
        },
    )

    found = await eligibility.find_recs_for_location(0.05, 0.2)

    assert len(calls) == 1, "three communities, one address lookup"
    assert [r["slug"] for r in found["matches"]] == ["a", "b", "c"]
    assert found["unchecked"] is False
