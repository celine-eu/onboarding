from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from celine.onboarding.api.deps import valid_rec_slug
from celine.onboarding.services.eligibility import geocode_address, get_checker

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
    if req.lat is not None and req.lng is not None:
        lat, lng = req.lat, req.lng
    elif req.address:
        try:
            geo = await geocode_address(req.address)
        except ValueError as e:
            raise HTTPException(404, str(e))
        lat, lng = geo.lat, geo.lng
    else:
        raise HTTPException(400, "Provide lat/lng or address")

    checker = get_checker(rec_slug)
    result = checker.check(lat, lng)

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
