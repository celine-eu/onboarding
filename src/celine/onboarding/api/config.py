from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from celine.onboarding.services import template_service

router = APIRouter(tags=["config"])


@router.get("/config")
async def get_config():
    return template_service.get_config()


@router.get("/template/{path:path}")
async def get_template_file(path: str):
    file_path = template_service.get_asset_path(path)
    if not file_path:
        raise HTTPException(404, "Asset not found")
    return FileResponse(file_path)
