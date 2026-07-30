"""The admin console API, mounted at `/api/admin`.

One prefix for the whole authenticated surface. That is what lets the ingress
guard it with a plain path matcher instead of a wildcard-segment pattern, and it
keeps `/api/{rec}/...` meaning "the public wizard" without exception.

The REC is a path segment *inside* this prefix — `/api/admin/{rec_slug}/...` —
rather than in front of it, which is where it used to be.
"""

from fastapi import APIRouter

from celine.onboarding.api.admin import (
    audit,
    documents,
    enablement,
    exports,
    me,
    recs,
    stats,
    submissions,
)


def create_admin_router() -> APIRouter:
    router = APIRouter(prefix="/api/admin")

    # Literal-path routers first: `/recs` and `/me` must not be shadowed by a
    # `{rec_slug}` pattern. `deps.RESERVED_SLUGS` is checked at startup so a REC
    # actually named "recs" is a boot failure rather than a silent 404.
    router.include_router(me.router)
    router.include_router(recs.router)

    router.include_router(submissions.router)
    router.include_router(enablement.router)
    router.include_router(documents.router)
    router.include_router(exports.router)
    router.include_router(stats.router)
    router.include_router(audit.router)
    return router


__all__ = ["create_admin_router"]
