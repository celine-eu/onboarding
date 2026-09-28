"""The communities an operator may administer."""

from __future__ import annotations

from typing import Annotated

from celine.sdk.auth import JwtUser
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from celine.onboarding.api.admin.deps import (
    DbDep,
    IpDep,
    RecDep,
    UserDep,
    require,
    require_global,
)
from celine.onboarding.api.admin.me import RecAccess, _accessible_recs
from celine.onboarding.security.policy import Capability
from celine.onboarding.services import audit_service, registry_sync, template_service
from celine.onboarding.services.audit_service import Actor

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


# ---------------------------------------------------------------------------
# Registry sync (REQ-0009 to REQ-0015)
# ---------------------------------------------------------------------------


class SyncItem(BaseModel):
    """One area or topology node, and what the sync did or would do to it.

    ``outcome`` is ``created``, ``changed``, ``unchanged``, ``refused``,
    ``deleted``, ``undeclared`` (a registry area the template no longer
    declares, left in place without ``prune``), ``renamed`` (a registry area
    moved to this key from ``renamed_from``, with its members) or ``not_run``.
    In a dry run it is what a real run would do.
    """

    key: str
    boundary_id: str | None = None
    outcome: str
    code: str | None = None
    reason: str | None = None
    #: An undeclared area: the members it holds. A renamed one: the members
    #: that moved with it (in a dry run, that would).
    members: int | None = None
    #: A renamed area: the registry key it is moved from.
    renamed_from: str | None = None


class SetupStepOut(BaseModel):
    """The community's set-up through the provisioning reconcile.

    ``status``: ``succeeded``, ``failed`` (with ``reason``), ``skipped`` (no
    provisioning service configured) or ``not_run`` (a dry run).
    """

    status: str
    code: str | None = None
    reason: str | None = None
    members: int | None = None
    created: int | None = None


class RegistrySyncOut(BaseModel):
    rec: str
    community: str
    dry_run: bool
    prune: bool
    #: Every area and node written or left alone as planned; the set-up step is
    #: reported apart and does not decide it.
    ok: bool
    setup: SetupStepOut
    nodes: list[SyncItem]
    areas: list[SyncItem]
    summary: dict[str, dict[str, int]]


class DriftArea(BaseModel):
    key: str
    boundary_id: str | None = None
    #: ``matches``, ``missing`` (not in the registry), ``differs``, or
    #: ``undeclared`` (in the registry, not in the template).
    state: str
    #: Registry areas holding this area's boundary under another key.
    held_by: list[str] = []


class DriftNode(BaseModel):
    key: str
    state: str


class RegistryDriftOut(BaseModel):
    rec: str
    community: str | None
    #: ``matches``, ``drift``, or ``not_synced`` (a template whose areas are
    #: not boundaries: nothing of it is synced).
    status: str
    areas: list[DriftArea]
    nodes: list[DriftNode]


def _refused(exc: registry_sync.SyncRefusedError) -> HTTPException:
    return HTTPException(exc.status_code, {"code": exc.code, "message": exc.detail})


@router.post("/recs/{rec_slug}/registry-sync", response_model=RegistrySyncOut)
async def registry_sync_route(
    rec_slug: RecDep,
    user: Annotated[JwtUser, Depends(require(Capability.RECS_WRITE))],
    db: DbDep,
    ip: IpDep,
    dry_run: bool = False,
    prune: bool = False,
) -> RegistrySyncOut:
    """Push this REC's template areas to its registry community.

    Realm-level `admins` only (`recs.write`); no service account holds it. A dry
    run writes nothing — not to the registry, not to the provisioning service —
    and answers the plan. A real run first sets the community up through the
    provisioning reconcile, whose failure is reported and does not stop the
    area writes. The status is `200` whenever the sync ran; `ok` and each item's
    `outcome` say what happened.
    """
    try:
        report = await registry_sync.sync(rec_slug, dry_run=dry_run, prune=prune)
    except registry_sync.SyncRefusedError as exc:
        raise _refused(exc) from None

    if not dry_run:
        await audit_service.record_and_commit(
            db,
            action="registry_sync",
            entity_type="rec",
            entity_id=rec_slug,
            actor=Actor.from_user(user),
            rec_slug=rec_slug,
            ip=ip,
            detail=report.audit_detail(),
        )
    return RegistrySyncOut.model_validate(report.to_dict())


@router.get("/recs/{rec_slug}/registry-drift", response_model=RegistryDriftOut)
async def registry_drift_route(
    rec_slug: RecDep,
    _: Annotated[JwtUser, Depends(require(Capability.RECS_DRIFT))],
) -> RegistryDriftOut:
    """Whether the registry's areas and topology match this REC's template.

    `recs.drift`: realm-level `admins`, and that REC's own `managers` and
    `admins` (D55); not its editors or viewers, and no service account.

    Reads the registry community with this service's own `rec-registry.read`;
    writes nothing and asks the Digital Twin nothing.
    """
    try:
        return RegistryDriftOut.model_validate(await registry_sync.drift(rec_slug))
    except registry_sync.SyncRefusedError as exc:
        raise _refused(exc) from None
