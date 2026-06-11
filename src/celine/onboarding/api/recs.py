from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException

from celine.onboarding.api.deps import require_admin
from celine.onboarding.services import template_service
from celine.onboarding.services.eligibility import find_recs_for_location, geocode_address

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

    return find_recs_for_location(lat, lng)


@router.post("/recs/reload", dependencies=[Depends(require_admin)])
async def reload_templates():
    await template_service.reload()
    slugs = template_service.get_slugs()
    return {"reloaded": len(slugs), "slugs": slugs}


