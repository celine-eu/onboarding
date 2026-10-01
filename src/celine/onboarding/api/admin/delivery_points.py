"""The community's shared delivery points: PODs more than one active member holds.

A POD is one grid connection, so two active members holding it is a mistake —
usually one person typed somebody else's code. The registry refuses to give such
a point again (`delivery_point_held`) until it is resolved; this lists the ones
already shared, from the registry's per-community report (registry plan F8), so an
operator can check them with the members and correct the wrong one by revision
(`docs/admin-console.md`).

**`submissions.revise`, not `submissions.read`.** The list exists to be acted on,
and the act is a revision; it names member keys beside each other's PODs, which
is review work, not queue reading. The same operators (`managers`, `admins`) hold
it as hold `submissions.review`.

The PODs are masked as on a submission and in its corrections, unless `reveal` is
asked for under `submissions.reveal`, which is audited as its own action. Holders
in other communities are a count, never a member or a community: the registry
never says more.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import httpx
from celine.sdk.auth import JwtUser
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from celine.onboarding.api.admin.deps import (
    ActorDep,
    DbDep,
    IpDep,
    RecDep,
    organization_of,
    require,
)
from celine.onboarding.api.admin.masking import mask_value
from celine.onboarding.models.submission import Submission
from celine.onboarding.security.policy import Capability, get_policy
from celine.onboarding.services import audit_service, rec_registry

router = APIRouter(tags=["admin"])

ReviseDep = Annotated[JwtUser, Depends(require(Capability.SUBMISSIONS_REVISE))]


class SharedHolder(BaseModel):
    member_key: str
    #: The point as this member's registry entry spells it; masked unless revealed.
    id: str
    #: The onboarding submission this member came from, when there is one here:
    #: the registry keys a member onboarded through this service on its
    #: submission's ref.
    submission_id: uuid.UUID | None = None
    submission_ref: str | None = None


class SharedDeliveryPoint(BaseModel):
    #: The point in the registry's compared form (trimmed, lower-cased); masked
    #: unless revealed.
    delivery_point: str
    holders: list[SharedHolder]
    #: Active members of other communities holding it: a count, nothing more.
    held_elsewhere: int
    active_holders: int


class SharedDeliveryPointsRead(BaseModel):
    community_key: str
    revealed: bool
    items: list[SharedDeliveryPoint]


async def _submissions_by_ref(db, rec_slug: str, refs: set[str]) -> dict[str, Submission]:
    if not refs:
        return {}
    result = await db.execute(
        select(Submission).where(Submission.rec_slug == rec_slug, Submission.ref.in_(refs))
    )
    return {s.ref: s for s in result.scalars().all()}


@router.get(
    "/{rec_slug}/delivery-points/shared",
    response_model=SharedDeliveryPointsRead,
)
async def shared_delivery_points(
    user: ReviseDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
    reveal: bool = Query(
        False,
        description="Unmask the PODs. Requires `submissions.reveal`, and is recorded in "
        "the audit trail.",
    ),
):
    """PODs that more than one active member holds, this community's holders first.

    409 when this community has no registry to ask; 404 when the registry holds no
    community for it; 502 for another registry refusal; 503 when it cannot be
    reached.
    """
    if reveal:
        decision = get_policy().allow(
            user, Capability.SUBMISSIONS_REVEAL, organization=organization_of(rec_slug)
        )
        if not decision.allowed:
            raise HTTPException(
                403,
                f"Revealing the PODs requires the reveal capability: "
                f"{decision.reason or 'access denied'}",
            )

    from celine.sdk.rec_registry import RecRegistryApiError

    try:
        report = await rec_registry.shared_delivery_points(rec_slug)
    except rec_registry.RegistryNotConfiguredError as exc:
        raise HTTPException(409, f"No REC registry to ask for this community: {exc}") from exc
    except RecRegistryApiError as exc:
        # The registry's own text is not passed on: status only.
        if exc.status_code == 404:
            raise HTTPException(404, "The REC registry holds no community for this REC") from exc
        raise HTTPException(
            502, f"The REC registry refused the report ({exc.status_code})"
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(503, "The REC registry could not be reached") from exc

    refs = {holder.member_key for item in report.items for holder in item.holders}
    submissions = await _submissions_by_ref(db, rec_slug, refs)

    def shown(value: str) -> str:
        return value if reveal else (mask_value(value) or value)

    items = [
        SharedDeliveryPoint(
            delivery_point=shown(item.delivery_point),
            holders=[
                SharedHolder(
                    member_key=holder.member_key,
                    id=shown(holder.id),
                    submission_id=submissions[holder.member_key].id
                    if holder.member_key in submissions
                    else None,
                    submission_ref=holder.member_key if holder.member_key in submissions else None,
                )
                for holder in item.holders
            ],
            held_elsewhere=item.held_elsewhere,
            active_holders=item.active_holders,
        )
        for item in report.items
    ]

    # Who looked, and how many points; never which.
    await audit_service.record_and_commit(
        db,
        action="reveal" if reveal else "view",
        entity_type="shared_delivery_points",
        entity_id=None,
        actor=actor,
        rec_slug=rec_slug,
        ip=ip,
        detail=f"points={len(items)}" + (" pod unmasked" if reveal else ""),
    )
    return SharedDeliveryPointsRead(
        community_key=report.community_key, revealed=reveal, items=items
    )
