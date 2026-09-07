"""The member's own surface — `/api/me/*`.

**Authorised by the member's token and nothing else.** Every other authenticated
route in this service names a `Capability` and checks it against the community in
the path; these name none, because there is no operator here. The member is
acting on their own consent, and an operator acting on it for them would make the
record worthless. `get_current_user` is shared with the admin surface — it
verifies a token and nothing more — but `require_capability` is deliberately
absent.

**Mounted before the `/api/{rec_slug}` routers**, not after. `main.py` mounts the
admin router last so its literal paths cannot be shadowed; a router added at the
end would be shadowed instead, since `{rec_slug}` matches `me`. `RESERVED_SLUGS`
already refuses a REC called `me` at startup, so the collision cannot arrive from
a manifest either.

The REC is **not** a path segment here, unlike everywhere else in this service. A
member does not choose which community they are in — their token says — and
accepting it from the caller would let anyone ask about any community's offers.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from celine.sdk.auth import JwtUser
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from celine.onboarding.api.admin.deps import get_current_user
from celine.onboarding.services import member_sharing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me", tags=["me"])

MemberDep = Annotated[JwtUser, Depends(get_current_user)]


class DataSharingDecisionRequest(BaseModel):
    """Grant or withdraw one offer."""

    enabled: bool


class DataSharingStatusResponse(BaseModel):
    """Every offer this member's community publishes, with their decision on it.

    ``has_identity`` and ``offers`` are the shape `../celine-webapp` already
    serves to its frontend, kept so a relocation costs the UI nothing. ``state``
    is additive and says *why* `has_identity` is false — "your community is not
    in a dataspace" and "you have no credential yet" need different sentences and
    used to get the same one.

    ``identity`` carries the member's DID, their credential's role, and its
    issued/expires dates — enough to quote to a REC manager looking them up, and
    the only way a member learns a DID that was minted on their behalf. ``None``
    unless `state` is `ok`.

    **No credential is ever in here.** Not the `vc_jws`, which authenticates as
    the member, and not in any field added later.
    """

    has_identity: bool
    state: member_sharing.SharingState
    offers: list[dict[str, Any]] = Field(default_factory=list)
    identity: dict[str, Any] | None = None


class DataSharingHistoryResponse(BaseModel):
    """The member's own record of what happened with their data."""

    has_identity: bool
    state: member_sharing.SharingState
    events: list[dict[str, Any]] = Field(default_factory=list)


def _unavailable(exc: Exception) -> HTTPException:
    """503, and the reason.

    Distinct from every :class:`~celine.onboarding.services.member_sharing.SharingState`,
    which are all answers rather than failures: an unreachable connector is worth
    retrying and "your community does not take part" never is.
    """
    return HTTPException(status_code=503, detail=str(exc))


@router.get("/data-sharing", response_model=DataSharingStatusResponse)
async def get_data_sharing(user: MemberDep) -> DataSharingStatusResponse:
    """What this member is sharing, and what they could share.

    Offers come from the published vocabulary through the REC's allow-list, and
    decisions from the connector under the member's own credential, so what is
    shown here and what the dataspace enforces cannot drift.
    """
    try:
        view = await member_sharing.get_data_sharing(user)
    except member_sharing.SharingUnavailableError as exc:
        raise _unavailable(exc) from exc

    return DataSharingStatusResponse(
        has_identity=view.has_identity,
        state=view.state,
        offers=view.offers,
        identity=view.identity,
    )


@router.post("/data-sharing/{offer_id}", response_model=DataSharingStatusResponse)
async def set_data_sharing(
    offer_id: str, body: DataSharingDecisionRequest, user: MemberDep
) -> DataSharingStatusResponse:
    """Grant or withdraw one offer.

    Withdrawal is the reason this route exists: the onboarding wizard can only
    grant, so without it a consent could be given and never taken back — which
    Art. 7(3) requires to be as easy as giving it.
    """
    try:
        view = await member_sharing.set_data_sharing(user, offer_id, enabled=body.enabled)
    except member_sharing.CannotDecideError as exc:
        # 409, not 403: nothing about this member's authorisation is wrong. They
        # are in a state where the decision does not exist to be made, and the
        # state says which.
        raise HTTPException(status_code=409, detail=str(exc.state)) from exc
    except ValueError as exc:
        # An offer this REC does not publish, or one disclosed under a contract
        # rather than consented to. Both name the offer.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except member_sharing.SharingUnavailableError as exc:
        raise _unavailable(exc) from exc

    return DataSharingStatusResponse(
        has_identity=view.has_identity,
        state=view.state,
        offers=view.offers,
        identity=view.identity,
    )


@router.get("/data-sharing/history", response_model=DataSharingHistoryResponse)
async def get_data_sharing_history(user: MemberDep) -> DataSharingHistoryResponse:
    """What has happened with this member's data, from their own record.

    Absent provenance returns an empty list rather than failing: the decisions
    stand without their history, and refusing the whole page for a detail is
    worse than showing it without one.
    """
    try:
        state, events = await member_sharing.get_history(user)
    except member_sharing.SharingUnavailableError as exc:
        raise _unavailable(exc) from exc

    return DataSharingHistoryResponse(
        has_identity=state is member_sharing.SharingState.OK, state=state, events=events
    )
