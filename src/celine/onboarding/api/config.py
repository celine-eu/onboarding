from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from celine.onboarding.api.deps import valid_rec_slug
from celine.onboarding.config.settings import settings
from celine.onboarding.services import provisioning, template_service

router = APIRouter(tags=["config"])


@router.get("/config")
async def get_config(rec_slug: str = Depends(valid_rec_slug)):
    """The manifest's public allow-list, and whether approval brings a login.

    ``login_invitation`` is what lets the wizard tell the person that approval
    comes with an email to set a password — and not tell them when it does not.
    Approval sends that invitation only where a login is provisioned at all: a
    deployment with no provisioning service, or a REC declaring no
    ``rec_registry`` block, gets no account and therefore no email. A promise
    the platform then breaks is worse than saying nothing.

    ``features`` is what the deployment allows, which the manifest cannot know.
    The wizard renders upload and scanning only where these say so, rather than
    offering a control the API would refuse. Upload and scan are separate
    fields that follow one switch today, so that upload without scanning can be
    enabled later without changing this response.
    """
    documents = settings.document_processing_enabled
    return {
        **template_service.get_config(rec_slug),
        "login_invitation": await provisioning.login_is_provisioned(rec_slug),
        "features": {"document_upload": documents, "document_scan": documents},
    }


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
    except template_service.SharingOffersUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/template/{path:path}")
async def get_template_file(path: str, rec_slug: str = Depends(valid_rec_slug)):
    file_path = template_service.get_asset_path(rec_slug, path)
    if not file_path:
        raise HTTPException(404, "Asset not found")
    return FileResponse(file_path)
