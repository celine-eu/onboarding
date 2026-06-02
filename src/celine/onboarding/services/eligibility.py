from dataclasses import dataclass, field
from typing import Protocol

import httpx

from celine.onboarding.services.template_service import load_manifest


@dataclass
class AddressInfo:
    lat: float
    lng: float
    display_name: str
    municipality: str | None = None
    postal_code: str | None = None
    county: str | None = None
    state: str | None = None
    country_code: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class EligibilityResult:
    eligible: bool
    matched_rule: str | None = None
    matched_value: str | None = None
    address: AddressInfo | None = None
    reason: str | None = None


RULE_FIELD_MAP = {
    "municipality": "municipality",
    "postal_code": "postal_code",
    "county": "county",
    "state": "state",
    "country_code": "country_code",
}


class EligibilityChecker(Protocol):
    def check(self, lat: float, lng: float) -> EligibilityResult: ...


class RulesChecker:
    def __init__(self, rules: list[dict]):
        self._rules = []
        for rule in rules:
            rtype = rule.get("type", "")
            values = {v.strip().lower() for v in rule.get("values", [])}
            if rtype in RULE_FIELD_MAP and values:
                self._rules.append((rtype, RULE_FIELD_MAP[rtype], values))

    def check(self, lat: float, lng: float) -> EligibilityResult:
        addr = reverse_geocode(lat, lng)

        for rule_type, addr_field, values in self._rules:
            actual = getattr(addr, addr_field, None)
            if actual and actual.strip().lower() in values:
                return EligibilityResult(
                    eligible=True,
                    matched_rule=rule_type,
                    matched_value=actual,
                    address=addr,
                )

        return EligibilityResult(
            eligible=False,
            address=addr,
            reason=f"{addr.municipality or addr.display_name} is not in the coverage area",
        )


class NoRestrictionChecker:
    def check(self, lat: float, lng: float) -> EligibilityResult:
        return EligibilityResult(eligible=True)


def get_checker() -> EligibilityChecker:
    manifest = load_manifest()
    coverage = manifest.get("coverage")
    if not coverage:
        return NoRestrictionChecker()

    rules = coverage.get("rules", [])

    if not rules and coverage.get("type") == "municipalities":
        rules = [{"type": "municipality", "values": coverage.get("municipalities", [])}]

    if not rules:
        return NoRestrictionChecker()

    return RulesChecker(rules)


def _parse_address(addr: dict) -> dict:
    return {
        "municipality": (
            addr.get("city")
            or addr.get("town")
            or addr.get("village")
            or addr.get("municipality")
        ),
        "postal_code": addr.get("postcode"),
        "county": addr.get("county"),
        "state": addr.get("state"),
        "country_code": addr.get("country_code", "").upper() or None,
    }


async def geocode_address(address: str) -> AddressInfo:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1, "addressdetails": 1},
            headers={"User-Agent": "cer-onboarding/0.1"},
        )
        resp.raise_for_status()
        results = resp.json()

    if not results:
        raise ValueError(f"Address not found: {address}")

    hit = results[0]
    parsed = _parse_address(hit.get("address", {}))

    return AddressInfo(
        lat=float(hit["lat"]),
        lng=float(hit["lon"]),
        display_name=hit.get("display_name", ""),
        raw=hit.get("address", {}),
        **parsed,
    )


def reverse_geocode(lat: float, lng: float) -> AddressInfo:
    with httpx.Client() as client:
        resp = client.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lng, "format": "json", "addressdetails": 1},
            headers={"User-Agent": "cer-onboarding/0.1"},
        )
        resp.raise_for_status()
        data = resp.json()

    parsed = _parse_address(data.get("address", {}))

    return AddressInfo(
        lat=lat,
        lng=lng,
        display_name=data.get("display_name", ""),
        raw=data.get("address", {}),
        **parsed,
    )
