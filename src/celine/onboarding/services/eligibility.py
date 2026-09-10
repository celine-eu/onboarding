from dataclasses import dataclass, field
from typing import Protocol

import httpx

from celine.onboarding.services import template_service


@dataclass
class AddressInfo:
    lat: float
    lng: float
    display_name: str
    municipality: str | None = None
    # Every administrative name the geocoder returned that could be the comune,
    # in the order `municipality` picks one for display. See MUNICIPALITY_KEYS.
    municipality_candidates: tuple[str, ...] = ()
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


# **OSM has no single key for "the comune", and in Trentino the obvious one is
# wrong.** `municipality` there is the *Comunità di valle*, a body above the
# comune: Volano comes back as `village=Volano, municipality=Comunità della
# Vallagarina`, and Folgaria as `city=Folgaria, municipality=Magnifica Comunità
# degli Altipiani Cimbri`. A frazione is the mirror image — Gionghi is
# `village=Gionghi, municipality=Lavarone`, and Lavarone is the comune the
# coverage rule names.
#
# So neither order works: preferring `village` rejects every frazione, and
# preferring `municipality` rejects nine of Green Land's eleven comuni. A
# `municipality` rule is therefore matched against **all** of these, and only
# the display value is chosen by precedence.
MUNICIPALITY_KEYS = ("city", "town", "village", "municipality")

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
            if rule_type == "municipality":
                candidates = addr.municipality_candidates
            else:
                one = getattr(addr, addr_field, None)
                candidates = (one,) if one else ()

            for actual in candidates:
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


def get_checker(rec_slug: str) -> EligibilityChecker:
    manifest = template_service.load_manifest(rec_slug)
    coverage = manifest.get("coverage")
    if not coverage:
        return NoRestrictionChecker()

    rules = coverage.get("rules", [])

    if not rules and coverage.get("type") == "municipalities":
        rules = [{"type": "municipality", "values": coverage.get("municipalities", [])}]

    if not rules:
        return NoRestrictionChecker()

    return RulesChecker(rules)


def find_recs_for_location(lat: float, lng: float) -> list[dict]:
    results = []
    for slug in template_service.get_slugs():
        checker = get_checker(slug)
        result = checker.check(lat, lng)
        if result.eligible:
            manifest = template_service.load_manifest(slug)
            results.append(
                {
                    "slug": slug,
                    "name": manifest.get("name", slug),
                    "branding": manifest.get("branding", {}),
                    "locale": manifest.get("locale", "it"),
                    "matched_rule": result.matched_rule,
                    "matched_value": result.matched_value,
                }
            )
    return results


def _parse_address(addr: dict) -> dict:
    candidates = tuple(dict.fromkeys(v for k in MUNICIPALITY_KEYS if (v := addr.get(k))))
    return {
        "municipality": candidates[0] if candidates else None,
        "municipality_candidates": candidates,
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
            headers={"User-Agent": "rec-onboarding/0.1"},
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
            headers={"User-Agent": "rec-onboarding/0.1"},
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
