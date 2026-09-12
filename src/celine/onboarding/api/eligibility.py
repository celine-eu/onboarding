from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from celine.onboarding.api.deps import valid_rec_slug
from celine.onboarding.services.eligibility import (
    GeocoderUnavailableError,
    geocode_address,
    get_checker,
    reverse_geocode,
)

router = APIRouter(tags=["eligibility"])


class EligibilityRequest(BaseModel):
    lat: float | None = None
    lng: float | None = None
    address: str | None = None


class EligibilityResponse(BaseModel):
    eligible: bool
    lat: float | None = None
    lng: float | None = None
    municipality: str | None = None
    postal_code: str | None = None
    state: str | None = None
    country_code: str | None = None
    matched_rule: str | None = None
    matched_value: str | None = None
    reason: str | None = None


@router.post("/eligibility", response_model=EligibilityResponse)
async def check_eligibility(
    req: EligibilityRequest,
    rec_slug: str = Depends(valid_rec_slug),
):
    checker = get_checker(rec_slug)

    # **The address is resolved once, here, and handed to the checker.** An
    # address request used to be geocoded for its coordinates, which were then
    # reverse-geocoded back into the address that had just been discarded: two
    # calls to the same public service per check, and the second one is what
    # timed out. A community with no coverage rules needs neither.
    addr = None
    try:
        if req.lat is not None and req.lng is not None:
            lat, lng = req.lat, req.lng
            if checker.requires_address:
                addr = await reverse_geocode(lat, lng)
        elif req.address:
            addr = await geocode_address(req.address)
            lat, lng = addr.lat, addr.lng
        else:
            raise HTTPException(400, "Provide lat/lng or address")
    except ValueError as e:
        raise HTTPException(404, str(e))
    except GeocoderUnavailableError as e:
        # Not a 500, and not `eligible: false`. The service this depends on did
        # not answer; saying the applicant is outside the area would be an
        # answer nobody computed.
        raise HTTPException(503, f"Address service unavailable: {e}")

    result = checker.check(addr)

    addr = result.address
    return EligibilityResponse(
        eligible=result.eligible,
        lat=lat,
        lng=lng,
        municipality=addr.municipality if addr else None,
        postal_code=addr.postal_code if addr else None,
        state=addr.state if addr else None,
        country_code=addr.country_code if addr else None,
        matched_rule=result.matched_rule,
        matched_value=result.matched_value,
        reason=result.reason,
    )
