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
    #: Whether this checker reads the address, so a caller knows to resolve one.
    #: A community with no coverage rules admits everybody, and asking the
    #: geocoder where somebody is in order to ignore the answer is a network
    #: call, a rate-limit slot and a failure mode bought for nothing.
    requires_address: bool

    def check(self, addr: AddressInfo | None) -> EligibilityResult: ...


class RulesChecker:
    #: A checker matches an address against rules; it does not fetch one. It
    #: used to take coordinates and reverse-geocode them itself, which cost a
    #: round trip per checker — and on the address path a *second* one, because
    #: the caller had already geocoded the address and thrown the result away to
    #: pass the two numbers down. Both calls went to the same public geocoder
    #: under the same User-Agent, which is how a coverage check came to depend on
    #: staying under someone else's rate limit twice.
    requires_address = True

    def __init__(self, rules: list[dict]):
        self._rules = []
        for rule in rules:
            rtype = rule.get("type", "")
            values = {v.strip().lower() for v in rule.get("values", [])}
            if rtype in RULE_FIELD_MAP and values:
                self._rules.append((rtype, RULE_FIELD_MAP[rtype], values))

    def check(self, addr: AddressInfo | None) -> EligibilityResult:
        if addr is None:
            raise ValueError("RulesChecker needs an address to match against")

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
    requires_address = False

    def check(self, addr: AddressInfo | None) -> EligibilityResult:
        return EligibilityResult(eligible=True, address=addr)


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


async def find_recs_for_location(
    lat: float, lng: float, addr: AddressInfo | None = None
) -> list[dict]:
    """Every community whose coverage admits this location.

    One address lookup for the whole sweep, not one per community: the
    coordinates are the same for all of them, so asking the geocoder once per
    checker returned the same answer N times. Pass ``addr`` when the caller has
    already resolved it — the address path always has.
    """
    checkers = [(slug, get_checker(slug)) for slug in template_service.get_slugs()]

    if addr is None and any(c.requires_address for _, c in checkers):
        addr = await reverse_geocode(lat, lng)

    results = []
    for slug, checker in checkers:
        result = checker.check(addr)
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


#: The geocoder is a third party on the public internet, reached while somebody
#: waits on a wizard step. httpx defaults to five seconds and no explicit value
#: at all, which is the same number written down nowhere — and a read timeout
#: leaving this module as an `httpx` exception reached the endpoint as a bare
#: `500`. Both are named here, and both surface as `GeocoderUnavailableError`.
_GEOCODER_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_GEOCODER_HEADERS = {"User-Agent": "rec-onboarding/0.1"}


class GeocoderUnavailableError(Exception):
    """The address service did not answer, so coverage cannot be decided.

    Distinct from "this address does not exist", which is a `ValueError` and a
    fact about the request. This one says nothing about the applicant, and the
    caller should say so rather than reporting them ineligible — a coverage
    answer nobody computed is not a refusal.
    """


async def geocode_address(address: str) -> AddressInfo:
    try:
        async with httpx.AsyncClient(timeout=_GEOCODER_TIMEOUT) as client:
            resp = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": address, "format": "json", "limit": 1, "addressdetails": 1},
                headers=_GEOCODER_HEADERS,
            )
            resp.raise_for_status()
            results = resp.json()
    except httpx.HTTPError as exc:
        raise GeocoderUnavailableError(f"address lookup failed: {exc}") from exc

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


async def reverse_geocode(lat: float, lng: float) -> AddressInfo:
    """The address at these coordinates.

    Async, and that is not a style choice: this ran on a synchronous `httpx`
    client from inside an async endpoint, so every coverage check blocked the
    worker — and the whole service with it — for as long as the geocoder took to
    answer, up to the timeout.
    """
    try:
        async with httpx.AsyncClient(timeout=_GEOCODER_TIMEOUT) as client:
            resp = await client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lng, "format": "json", "addressdetails": 1},
                headers=_GEOCODER_HEADERS,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise GeocoderUnavailableError(f"reverse lookup failed: {exc}") from exc

    parsed = _parse_address(data.get("address", {}))

    return AddressInfo(
        lat=lat,
        lng=lng,
        display_name=data.get("display_name", ""),
        raw=data.get("address", {}),
        **parsed,
    )
