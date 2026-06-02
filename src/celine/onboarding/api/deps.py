from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from slowapi import Limiter
from slowapi.util import get_remote_address

from celine.onboarding.config.settings import settings

limiter = Limiter(key_func=get_remote_address)

_admin_header = APIKeyHeader(name="Authorization", auto_error=False)


async def require_admin(api_key: str | None = Security(_admin_header)) -> str:
    if not settings.admin_token:
        raise HTTPException(503, "Admin access not configured")
    if not api_key:
        raise HTTPException(401, "Authorization header required")
    token = api_key.removeprefix("Bearer ").strip()
    if token != settings.admin_token:
        raise HTTPException(403, "Invalid admin token")
    return token
