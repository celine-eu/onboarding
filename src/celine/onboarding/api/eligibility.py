from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from celine.onboarding.api.deps import limiter, valid_rec_slug
from celine.onboarding.config.settings import settings
from celine.onboarding.services.boundaries import DigitalTwinUnavailableError
from celine.onboarding.services.eligibility import (
    GeocoderUnavailableError,
    evaluate,
    geocode_address,
    get_checker,
    reverse_geocode,
)

router = APIRouter(tags=["eligibility"])

#: What the wizard shows when the check could not be made. The same words for a
#: silent geocoder and a silent Digital Twin: the applicant can do nothing with
#: the difference, and neither is a verdict.
CANNOT_CHECK_NOW = "We cannot check your address right now. Please try again later."


class EligibilityRequest(BaseModel):
    lat: float | None = None
    lng: float | None = None
    address: str | None = None


class EligibilityResponse(BaseModel):
    # No `lat`/`lng` (REQ-0006): the point geocoded from the caller's address
    # decides the answer and is dropped, never handed back to an anonymous
    # caller.
    eligible: bool
    municipality: str | None = None
    postal_code: str | None = None
    state: str | None = None
    country_code: str | None = None
    # Never set for a community whose areas are boundaries: a matched value
    # there would name the boundary or the area (REQ-0006).
    matched_rule: str | None = None
    matched_value: str | None = None
    reason: str | None = None


@router.post("/eligibility", response_model=EligibilityResponse)
@limiter.limit(lambda: settings.rate_limit_eligibility)
async def check_eligibility(
    request: Request,
    req: EligibilityRequest,
    rec_slug: str = Depends(valid_rec_slug),
):
    # Anonymous, and rate-limited per client address (REQ-0016): each check
    # costs a geocoder call and, for a boundary community, a Digital Twin call
    # made with this service's own token. Over the limit the caller is answered
    # 429 before either is made. Nothing here writes anything.
    checker = get_checker(rec_slug)

    # **The address is resolved once, here, and handed to the checker.** An
    # address request used to be geocoded for its coordinates, which were then
    # reverse-geocoded back into the address that had just been discarded: two
    # calls to the same public service per check, and the second one is what
    # timed out. A community with no coverage rules needs neither.
    addr = None
    try:
        if req.lat is not None and req.lng is not None:
            if checker.requires_address:
                addr = await reverse_geocode(req.lat, req.lng)
        elif req.address:
            addr = await geocode_address(req.address)
        else:
            raise HTTPException(400, "Provide lat/lng or address")
    except ValueError as e:
        raise HTTPException(404, str(e))
    except GeocoderUnavailableError as e:
        # Not a 500, and not `eligible: false`. The service this depends on did
        # not answer; saying the applicant is outside the area would be an
        # answer nobody computed.
        raise HTTPException(503, f"Address service unavailable: {e}")

    try:
        result = await evaluate(checker, addr)
    except DigitalTwinUnavailableError:
        # The same answer as an unreachable geocoder (REQ-0005): the boundary
        # was not computed, so neither yes nor no, and nothing is kept.
        raise HTTPException(503, CANNOT_CHECK_NOW) from None

    addr = result.address
    return EligibilityResponse(
        eligible=result.eligible,
        municipality=addr.municipality if addr else None,
        postal_code=addr.postal_code if addr else None,
        state=addr.state if addr else None,
        country_code=addr.country_code if addr else None,
        matched_rule=result.matched_rule,
        matched_value=result.matched_value,
        reason=result.reason,
    )
