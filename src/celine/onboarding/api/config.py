from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from celine.onboarding.api.deps import valid_rec_slug
from celine.onboarding.services import template_service

router = APIRouter(tags=["config"])


@router.get("/config")
async def get_config(rec_slug: str = Depends(valid_rec_slug)):
    return template_service.get_config(rec_slug)


@router.get("/sharing-offers")
async def get_sharing_offers(rec_slug: str = Depends(valid_rec_slug)):
    """Public: the data-sharing offers the wizard renders (codes + English fallback).

    Answers 503 when the published vocabulary cannot be read, so the wizard can
    say the options are temporarily unavailable. An empty 200 means this
    community genuinely offers nothing — the two must not look alike, or a
    misconfiguration silently costs every consent in the window.
    """
    try:
        return await template_service.get_sharing_offers(rec_slug)
    except template_service.SharingOffersUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/template/{path:path}")
async def get_template_file(path: str, rec_slug: str = Depends(valid_rec_slug)):
    file_path = template_service.get_asset_path(rec_slug, path)
    if not file_path:
        raise HTTPException(404, "Asset not found")
    return FileResponse(file_path)
