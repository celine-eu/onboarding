from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from celine.onboarding.api.deps import valid_rec_slug
from celine.onboarding.services import template_service

router = APIRouter(tags=["config"])


@router.get("/config")
async def get_config(rec_slug: str = Depends(valid_rec_slug)):
    return template_service.get_config(rec_slug)


@router.get("/template/{path:path}")
async def get_template_file(path: str, rec_slug: str = Depends(valid_rec_slug)):
    file_path = template_service.get_asset_path(rec_slug, path)
    if not file_path:
        raise HTTPException(404, "Asset not found")
    return FileResponse(file_path)
