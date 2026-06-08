import secrets
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.config.settings import settings
from celine.onboarding.models.database import get_db

limiter = Limiter(key_func=get_remote_address)

SESSION_TTL_SECONDS = 600

_admin_header = APIKeyHeader(name="Authorization", auto_error=False)


async def require_admin(api_key: str | None = Security(_admin_header)) -> str:
    if not settings.admin_token:
        raise HTTPException(503, "Admin access not configured")
    if not api_key:
        raise HTTPException(401, "Authorization header required")
    token = api_key.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, settings.admin_token):
        raise HTTPException(403, "Invalid admin token")
    return token


async def require_session(request: Request, db: AsyncSession = Depends(get_db)):
    from celine.onboarding.models.submission import Submission

    token = request.headers.get("x-session-token", "")
    if not token:
        raise HTTPException(401, "Session token required")

    result = await db.execute(
        select(Submission).where(Submission.session_token == token)
    )
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(403, "Invalid session token")

    now = datetime.now(timezone.utc)
    anchor = submission.last_active_at or submission.created_at
    if anchor and (now - anchor).total_seconds() > SESSION_TTL_SECONDS:
        raise HTTPException(410, "Session expired. Please start a new submission.")

    submission.last_active_at = now
    await db.commit()
    return submission


