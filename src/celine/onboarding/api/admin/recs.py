"""The communities an operator may administer."""

from __future__ import annotations

from typing import Annotated

from celine.sdk.auth import JwtUser
from fastapi import APIRouter, Depends

from celine.onboarding.api.admin.deps import UserDep, require_global
from celine.onboarding.api.admin.me import RecAccess, _accessible_recs
from celine.onboarding.security.policy import Capability
from celine.onboarding.services import template_service

router = APIRouter(tags=["admin"])


@router.get("/recs", response_model=list[RecAccess])
async def list_accessible_recs(user: UserDep) -> list[RecAccess]:
    """Drives the console's community picker.

    Unlike `/me` this returns an empty list rather than 403 — a picker with
    nothing in it is a legitimate answer, and the caller has already been told by
    `/me` if they administer nothing.
    """
    return await _accessible_recs(user)


@router.post("/recs/reload")
async def reload_templates(
    _: Annotated[JwtUser, Depends(require_global(Capability.RECS_READ))],
) -> dict:
    """Force a manifest cache refresh.

    Deployment-wide, so it belongs to no community — only a realm-level operator
    (or a scoped service account) satisfies it. Gated on `recs.read` rather than a
    write capability because the cache refreshes itself on a 5-second TTL anyway;
    this only makes an operator stop waiting.

    Moved here from the public `/api/recs` router, which protected it with the
    shared admin token.
    """
    await template_service.reload()
    slugs = template_service.get_slugs()
    return {"reloaded": len(slugs), "slugs": slugs}
