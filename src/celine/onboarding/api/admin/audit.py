"""One community's audit trail."""

from __future__ import annotations

from typing import Annotated

from celine.sdk.auth import JwtUser
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from celine.onboarding.api.admin.deps import DbDep, RecDep, require
from celine.onboarding.models.audit_log import AuditLog
from celine.onboarding.models.schemas import AuditLogRead
from celine.onboarding.security.policy import Capability

router = APIRouter(tags=["admin"])

AuditReadDep = Annotated[JwtUser, Depends(require(Capability.AUDIT_READ))]


@router.get("/{rec_slug}/audit-logs", response_model=list[AuditLogRead])
async def list_audit_logs(
    _: AuditReadDep,
    db: DbDep,
    rec_slug: RecDep,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    """Scoped to the community in the path.

    The previous endpoint sat under `/api/{rec}/admin` but ignored the slug, so
    any token holder read the whole deployment's history. Rows written before
    `rec_slug` existed and not recovered by the 0009 backfill are excluded: they
    name no community, and showing them under an arbitrary one would invent the
    fact.

    Reading the trail is deliberately *not* itself audited. It is a read granted
    to every tier, and logging each view would bury the actions worth finding
    under the act of looking for them.
    """
    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.rec_slug == rec_slug)
        .order_by(AuditLog.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().all())
