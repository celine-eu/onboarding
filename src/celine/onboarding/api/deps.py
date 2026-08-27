from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.models.database import get_db
from celine.onboarding.services import template_service

limiter = Limiter(key_func=get_remote_address)

SESSION_TTL_SECONDS = 600


async def valid_rec_slug(rec_slug: str) -> str:
    await template_service.ensure_fresh()
    if rec_slug not in template_service.get_slugs():
        raise HTTPException(404, f"REC '{rec_slug}' not found")
    return rec_slug


async def require_session(
    request: Request,
    rec_slug: str = Depends(valid_rec_slug),
    db: AsyncSession = Depends(get_db),
):
    from celine.onboarding.models.submission import Submission

    token = request.headers.get("x-session-token", "")
    if not token:
        raise HTTPException(401, "Session token required")

    result = await db.execute(select(Submission).where(Submission.session_token == token))
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(403, "Invalid session token")

    if submission.rec_slug != rec_slug:
        raise HTTPException(403, "Session does not belong to this REC")

    now = datetime.now(UTC)
    anchor = submission.last_active_at or submission.created_at
    if anchor and (now - anchor).total_seconds() > SESSION_TTL_SECONDS:
        raise HTTPException(410, "Session expired. Please start a new submission.")

    submission.last_active_at = now
    await db.commit()
    return submission
