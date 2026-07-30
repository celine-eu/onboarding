"""Admin-path auth pre-check.

Rejects any `/api/admin/**` request that carries no recognisable token with 401
before it reaches a route handler, so the console's route shapes are not
discoverable without credentials.

This deliberately guards **only** the admin prefix. celine-grid's equivalent
middleware 401s every non-public path, which it can afford because every path is
authenticated; here the onboarding wizard is anonymous by design and must pass
through untouched.

Scope and organization checks are enforced per endpoint (see
`api/admin/deps.py`), not here.
"""

from __future__ import annotations

import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

ADMIN_PREFIX = "/api/admin"


def is_admin_path(path: str) -> bool:
    """True for the admin surface, false for anything that merely looks like it.

    Matched as an exact segment boundary rather than a bare prefix so that a
    future `/api/administrators` is not silently placed behind the gate — the
    same care the Caddy path matcher needs.
    """
    return path == ADMIN_PREFIX or path.startswith(f"{ADMIN_PREFIX}/")


def has_token(request: Request) -> bool:
    if request.headers.get("x-auth-request-access-token"):
        return True
    return request.headers.get("authorization", "").lower().startswith("bearer ")


class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if is_admin_path(request.url.path) and not has_token(request):
            logger.warning(
                "Unauthenticated admin request: %s %s from %s",
                request.method,
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            # 401, never a redirect: the console fetches this surface with XHR,
            # and a 302 to an HTML login page surfaces as a CORS failure rather
            # than something the client can act on.
            return JSONResponse({"detail": "Missing authentication token"}, status_code=401)
        return await call_next(request)
