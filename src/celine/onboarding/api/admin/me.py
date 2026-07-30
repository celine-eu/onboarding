"""Who the operator is, and what they may do.

The console asks this once at load and shapes itself around the answer, the same
way `apps/grid` does: a 403 here means "signed in, but not an operator of
anything", which the frontend turns into a denied page rather than a login loop.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from celine.onboarding.api.admin.deps import UserDep, organization_of
from celine.onboarding.security.policy import get_policy, organization_aliases, realm_groups
from celine.onboarding.services import template_service

router = APIRouter(tags=["admin"])


class RecAccess(BaseModel):
    slug: str
    name: str
    organization: str | None
    capabilities: list[str]


class AdminMe(BaseModel):
    sub: str
    email: str | None
    name: str | None
    preferred_username: str | None
    locale: str | None
    # "user" or "service" — the console shows a machine caller differently, and
    # the CLI's `whoami` prints it.
    subject_type: str
    organizations: list[str]
    realm_groups: list[str]
    recs: list[RecAccess]


@router.get("/ping", include_in_schema=False)
async def ping(user: UserDep) -> dict:
    """Liveness for the session guard. Authenticated, so a lapsed session 401s."""
    return {"ok": True}


async def _accessible_recs(user) -> list[RecAccess]:
    """Every REC this caller holds at least one capability on.

    Capabilities are resolved per REC rather than once: the whole point of the
    organization check is that the answer differs between communities. That is
    `len(Capability)` policy evaluations per REC, all in-process and sub-
    millisecond, against a REC count in the single digits.
    """
    await template_service.ensure_fresh()
    policy = get_policy()

    accessible: list[RecAccess] = []
    for slug in sorted(template_service.get_slugs()):
        organization = organization_of(slug)
        capabilities = policy.capabilities(user, organization=organization)
        if not capabilities:
            continue
        manifest = template_service.load_manifest(slug)
        accessible.append(
            RecAccess(
                slug=slug,
                name=manifest.get("name", slug),
                organization=organization,
                capabilities=sorted(capabilities),
            )
        )
    return accessible


@router.get("/me", response_model=AdminMe)
async def me(user: UserDep) -> AdminMe:
    """The caller's identity and per-community capabilities.

    403 when they administer nothing. A valid token that grants nothing is not an
    authentication problem, and answering 200-with-an-empty-list would send the
    console to a login screen it has already passed.
    """
    recs = await _accessible_recs(user)
    if not recs:
        raise HTTPException(
            403,
            "No community grants you access. Ask a REC administrator to add you to "
            "its Keycloak organization.",
        )

    claims = user.claims or {}
    return AdminMe(
        sub=user.sub,
        email=user.email,
        name=user.name,
        preferred_username=user.preferred_username,
        locale=claims.get("locale"),
        subject_type="service" if user.is_service_account else "user",
        organizations=organization_aliases(claims),
        realm_groups=realm_groups(claims),
        recs=recs,
    )
