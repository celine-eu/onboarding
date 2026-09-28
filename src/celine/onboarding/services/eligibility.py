import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from celine.onboarding.services import template_service

logger = logging.getLogger(__name__)


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


# **OSM has no single key for "the comune", and in some regions the obvious one
# is wrong.** Where a body sits above the comune (a valley or district union),
# `municipality` names that body: a town comes back as `village=Springfield,
# municipality=Union of the Valley`. A frazione is the mirror image — a hamlet
# comes back as `village=Shelbyville Heights, municipality=Shelbyville`, and
# Shelbyville is the comune the coverage rule names.
#
# So neither order works: preferring `village` rejects every frazione, and
# preferring `municipality` rejects most of such a community's comuni. A
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


#: What an address outside a boundary community is told. It names nothing: not
#: the boundary the point fell in, not an area, not the municipality (ADR-0013).
OUTSIDE_THE_COMMUNITY = "This address is not in the community's area"


class BoundaryChecker:
    """Eligibility by primary-substation boundary, through the Digital Twin (REQ-0003).

    Eligible only when the boundary covering the address's point is the boundary
    of one of the template's areas; outside every declared boundary, not
    eligible; no fallback to a municipality, to ``coverage.rules`` or to a
    default area. It is the one checker that does I/O, so it is asked through
    :func:`evaluate`, never :meth:`check`.

    The result carries **no boundary id, no area and no matched rule or value**
    (REQ-0006): an anonymous caller learns whether an address is inside, never
    where inside.
    """

    requires_address = True

    def __init__(self, binding: template_service.RecRegistryBinding):
        self.binding = binding

    def check(self, addr: AddressInfo | None) -> EligibilityResult:
        raise TypeError("BoundaryChecker asks the Digital Twin; use `await evaluate(...)`")

    async def check_async(self, addr: AddressInfo | None) -> EligibilityResult:
        """Raises ``DigitalTwinUnavailableError`` when the Digital Twin cannot answer."""
        from celine.onboarding.services.boundaries import resolve_point

        if addr is None:
            raise ValueError("BoundaryChecker needs an address to resolve")
        resolution = await resolve_point(self.binding, addr.lat, addr.lng)
        if resolution.eligible:
            return EligibilityResult(eligible=True, address=addr)
        return EligibilityResult(eligible=False, address=addr, reason=OUTSIDE_THE_COMMUNITY)


async def evaluate(checker: EligibilityChecker, addr: AddressInfo | None) -> EligibilityResult:
    """Ask *checker* about *addr*, whether or not it has to go over the network."""
    if isinstance(checker, BoundaryChecker):
        return await checker.check_async(addr)
    return checker.check(addr)


def get_checker(rec_slug: str) -> EligibilityChecker:
    # A template whose areas are boundaries has one eligibility answer, the
    # boundary's. Import refuses `coverage.rules` beside it (REQ-0017), and even
    # a manifest that slipped past import never reaches the rules below.
    manifest = template_service.load_manifest(rec_slug)
    if template_service.declares_boundaries(manifest.get("rec_registry")):
        return BoundaryChecker(template_service.rec_registry_binding(rec_slug))

    coverage = manifest.get("coverage")
    if not coverage:
        return NoRestrictionChecker()

    rules = coverage.get("rules", [])

    if not rules and coverage.get("type") == "municipalities":
        rules = [{"type": "municipality", "values": coverage.get("municipalities", [])}]

    if not rules:
        return NoRestrictionChecker()

    return RulesChecker(rules)


async def find_recs_for_location(lat: float, lng: float, addr: AddressInfo | None = None) -> dict:
    """Every community whose coverage admits this location, and whether any went unchecked.

    ``{"matches": [...], "unchecked": bool}``. ``unchecked`` is true when a
    community whose areas are boundaries could not be checked because the
    Digital Twin did not answer (REQ-0019): it is left out of ``matches``, and
    the caller can say "try again later" rather than "no community covers this
    address", which is not what was found.

    One address lookup for the whole sweep, not one per community: the
    coordinates are the same for all of them, so asking the geocoder once per
    checker returned the same answer N times. Pass ``addr`` when the caller has
    already resolved it — the address path always has.
    """
    checkers = [(slug, get_checker(slug)) for slug in template_service.get_slugs()]

    if addr is None and any(c.requires_address for _, c in checkers):
        addr = await reverse_geocode(lat, lng)

    from celine.onboarding.services.boundaries import DigitalTwinUnavailableError

    results = []
    unchecked = False
    for slug, checker in checkers:
        # A boundary community the Digital Twin cannot answer for fails closed
        # **on its own** (REQ-0019): it is not listed, since an admission nobody
        # computed is not an admission; and the other communities are still
        # answered, since a municipality community's check never needed the
        # Digital Twin. The point is not logged.
        try:
            result = await evaluate(checker, addr)
        except DigitalTwinUnavailableError:
            logger.warning(
                "find-by-address: community %s left out, its boundaries could not be checked",
                slug,
            )
            unchecked = True
            continue
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
    return {"matches": results, "unchecked": unchecked}


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
