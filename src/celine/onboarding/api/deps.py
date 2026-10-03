from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.config.settings import settings
from celine.onboarding.models.database import get_db
from celine.onboarding.services import template_service

#: Keyed by the connection's peer, as uvicorn resolved it (see :func:`peer_ip`).
limiter = Limiter(key_func=get_remote_address)


def peer_ip(request: Request) -> str:
    """The client's address, for consent evidence and the audit trail.

    The connection's peer as uvicorn resolved it, **never a request header**:
    ``X-Forwarded-For`` and ``X-Real-IP`` are written by whoever sends the
    request. Behind an ingress, uvicorn replaces the peer with the forwarded
    client address only when the connection comes from an address listed in
    ``FORWARDED_ALLOW_IPS``; from anywhere else the header is ignored
    (REQ-0020). The rate limiter keys on the same value.
    """
    return request.client.host if request.client else "unknown"


SESSION_TTL_SECONDS = 600

DOCUMENT_PROCESSING_DISABLED = "document_processing_disabled"
PHONE_VERIFICATION_DISABLED = "phone_verification_disabled"


async def require_document_processing() -> None:
    """Refuse a document upload or scan while the feature is switched off.

    The routes stay registered so the API has the same shape on every
    deployment; a caller learns the feature is off from a 403 with a stable
    ``code``, and the wizard never gets that far because ``/config`` already
    said so. See ``Settings.document_processing_enabled`` for what the switch is.
    """
    if not settings.document_processing_enabled:
        raise HTTPException(
            403,
            {
                "code": DOCUMENT_PROCESSING_DISABLED,
                "message": "Document upload and scanning are not enabled on this deployment.",
            },
        )


async def valid_rec_slug(rec_slug: str) -> str:
    await template_service.ensure_fresh()
    if rec_slug not in template_service.get_slugs():
        raise HTTPException(404, f"REC '{rec_slug}' not found")
    # The community's legal documents, when a legal host is set and they are due.
    from celine.onboarding.services import legal_documents

    await legal_documents.refresh(rec_slug, template_service.load_manifest(rec_slug))
    return rec_slug


async def require_phone_verification() -> None:
    """Refuse to send or confirm an SMS code while phone verification is off.

    Same shape as `require_document_processing`: the routes stay registered, the
    refusal carries a stable ``code``, and the wizard has already dropped the
    step because ``/config`` said so. See ``Settings.phone_verification_enabled``.
    """
    if not settings.phone_verification_enabled:
        raise HTTPException(
            403,
            {
                "code": PHONE_VERIFICATION_DISABLED,
                "message": "Phone verification is not enabled on this deployment.",
            },
        )


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
