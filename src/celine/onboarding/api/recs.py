from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from celine.onboarding.services import template_service
from celine.onboarding.services.eligibility import (
    GeocoderUnavailableError,
    find_recs_for_location,
    geocode_address,
)

router = APIRouter(tags=["recs"])


@router.get("/recs")
async def list_recs():
    await template_service.ensure_fresh()
    return template_service.get_all_recs_summary()


class FindByAddressRequest(BaseModel):
    address: str | None = None
    lat: float | None = None
    lng: float | None = None


@router.post("/recs/find-by-address")
async def find_recs_by_address(req: FindByAddressRequest):
    addr = None
    try:
        if req.lat is not None and req.lng is not None:
            lat, lng = req.lat, req.lng
        elif req.address:
            # Passed on rather than reduced to coordinates: the sweep below would
            # otherwise ask the geocoder to rebuild the address we already hold.
            addr = await geocode_address(req.address)
            lat, lng = addr.lat, addr.lng
        else:
            raise HTTPException(400, "Provide lat/lng or address")
    except ValueError as e:
        raise HTTPException(404, str(e))
    except GeocoderUnavailableError as e:
        raise HTTPException(503, f"Address service unavailable: {e}")

    return await find_recs_for_location(lat, lng, addr)


# `POST /recs/reload` moved to `POST /api/admin/recs/reload`. It was protected by
# the shared admin token, which no longer exists; every authenticated operation
# now lives under the one prefix the ingress guards.
