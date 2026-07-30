"""Queue counts for the console's landing view."""

from __future__ import annotations

from typing import Annotated

from celine.sdk.auth import JwtUser
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from celine.onboarding.api.admin.deps import DbDep, RecDep, require
from celine.onboarding.security.policy import Capability
from celine.onboarding.services import submission_service

router = APIRouter(tags=["admin"])

ReadDep = Annotated[JwtUser, Depends(require(Capability.SUBMISSIONS_READ))]


class RecStats(BaseModel):
    rec_slug: str
    # Every status, including the ones at zero — a hidden "0 awaiting review"
    # makes an empty queue indistinguishable from a broken filter.
    by_status: dict[str, int]
    # Approved participants with an enablement step still failing: enabled in
    # name only, and invisible to every pipeline downstream until repaired.
    submissions_with_failed_steps: int


@router.get("/{rec_slug}/stats", response_model=RecStats)
async def rec_stats(_: ReadDep, db: DbDep, rec_slug: RecDep) -> RecStats:
    by_status = await submission_service.queue_stats(db, rec_slug=rec_slug)
    enablement = await submission_service.enablement_stats(db, rec_slug=rec_slug)
    return RecStats(
        rec_slug=rec_slug,
        by_status=by_status,
        submissions_with_failed_steps=enablement["submissions_with_failed_steps"],
    )
