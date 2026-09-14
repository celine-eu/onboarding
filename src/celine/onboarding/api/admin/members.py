"""A manager emails a registry member, through the community dashboard.

Two routes, keyed on the **registry's** pair rather than on a REC slug and a
submission, because the member they address may have no submission at all: one
imported into the registry is as reachable here as one onboarded through the
wizard. Onboarding is the only caller of the provisioning service, so the
dashboard's "Send invitation" and "Reset password" arrive here and go on:

    dashboard ─▶ celine-community ─▶ these routes ─▶ provisioning ─▶ Keycloak

**The call is delegated.** The caller is a service holding
`onboarding.members.invite`, in `Authorization`, and it forwards the manager's own
access token in `X-Acting-User-Token`. Both are verified here, and the policy
allows the call only when the service holds the scope *and* the manager holds a
group granting `members.invite` on the REC. A manager cannot call these directly,
and no service can send without a person's decision behind it.

**Nothing is written locally except the audit row.** No submission is read, and
no step row changes. The provisioning service resolves the member through the
registry export.

Every error is `{"detail": {"code", "message"}}`. The codes are the contract; the
message is English for logs and the CLI. The provisioning service's own message is
never read or relayed: only its code, which passes through unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import httpx
import jwt as pyjwt
from celine.sdk.auth import JwtUser
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from celine.onboarding.api.admin.deps import DbDep, IpDep, organization_of
from celine.onboarding.config.settings import settings
from celine.onboarding.security.oidc import is_configured, oidc_settings
from celine.onboarding.security.policy import Capability, get_policy
from celine.onboarding.services import audit_service, provisioning, template_service
from celine.onboarding.services.audit_service import Actor
from celine.onboarding.services.errors import ConfigurationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/communities/{community}/members/{member_key}", tags=["admin"])

#: The header carrying the manager's own access token.
ACTING_USER_HEADER = "X-Acting-User-Token"

Intent = Literal["invitation", "password_reset"]

#: The audit action for each intent.
AUDIT_ACTIONS: dict[str, str] = {
    "invitation": "member_invitation",
    "password_reset": "member_password_reset",
}


def _refusal(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, **extra})


# ---------------------------------------------------------------------------
# Who is asking, and for whom
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Delegation:
    """A verified service, the verified operator it acts for, and the REC resolved."""

    service: JwtUser
    operator: JwtUser
    community: str
    rec_slug: str


def _verify(token: str, *, code: str, what: str) -> JwtUser:
    """Verify one token as the console does: this issuer's JWKS, `svc-onboarding`."""
    try:
        return JwtUser.from_token(token, oidc=oidc_settings())
    except pyjwt.InvalidTokenError as exc:
        raise _refusal(401, code, f"Invalid {what}: {exc}")
    except Exception as exc:
        logger.warning("Verifying the %s failed: %s", what, exc)
        raise _refusal(401, code, f"The {what} could not be verified.")


def _bearer(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


def _resolve_rec(community: str) -> str:
    slugs = template_service.recs_for_registry_community(community)
    if not slugs:
        raise _refusal(
            404,
            "community_not_served",
            f"No REC on this deployment files its members under registry community {community!r}.",
        )
    if len(slugs) > 1:
        logger.error(
            "Registry community %r is declared by more than one REC manifest (%s); "
            "refusing to choose which one's operators may email its members",
            community,
            ", ".join(sorted(slugs)),
        )
        raise _refusal(
            409,
            "community_ambiguous",
            f"More than one REC declares registry community {community!r}.",
        )
    return slugs[0]


async def require_delegated_invite(
    request: Request,
    community: Annotated[str, Path(min_length=1, max_length=100)],
) -> Delegation:
    """Authenticate both tokens, resolve the REC, and evaluate `members.invite`.

    Reads `Authorization` and `X-Acting-User-Token` explicitly, never the shared
    `_extract_token`, which prefers oauth2-proxy's header. A delegated call carrying
    that header would authenticate as the manager and skip the scope check, so a
    request carrying it is refused outright: it came through the public ingress,
    which is not the path this call takes.
    """
    if not is_configured():
        logger.error("Admin request rejected: OIDC is not configured")
        raise _refusal(503, "admin_not_configured", "Admin access is not configured.")

    if request.headers.get(settings.jwt_header_name):
        raise _refusal(
            401,
            "proxy_token_refused",
            f"A delegated call must not carry {settings.jwt_header_name}; call this "
            "service on its internal address.",
        )

    service_token = _bearer(request)
    if not service_token:
        raise _refusal(401, "invalid_token", "Missing service token in Authorization.")
    service = _verify(service_token, code="invalid_token", what="service token")

    acting_token = (request.headers.get(ACTING_USER_HEADER) or "").strip()
    if not acting_token:
        raise _refusal(401, "actor_token_invalid", f"Missing {ACTING_USER_HEADER}.")
    operator = _verify(acting_token, code="actor_token_invalid", what=ACTING_USER_HEADER)

    # Before the policy: no evaluation is spent on a call that cannot go anywhere.
    if not provisioning.provisioning_enabled():
        raise _refusal(
            503,
            "provisioning_not_configured",
            "This deployment has no provisioning service (PROVISIONING_URL is empty).",
        )

    await template_service.ensure_fresh()
    rec_slug = _resolve_rec(community)

    decision = get_policy().allow(
        service,
        Capability.MEMBERS_INVITE,
        organization=organization_of(rec_slug),
        actor=operator,
    )
    if not decision.allowed:
        raise _refusal(403, "forbidden", decision.reason or "access denied")

    return Delegation(service=service, operator=operator, community=community, rec_slug=rec_slug)


DelegationDep = Annotated[Delegation, Depends(require_delegated_invite)]
MemberKey = Annotated[str, Path(min_length=1, max_length=100)]


# ---------------------------------------------------------------------------
# What the provisioning service answered
# ---------------------------------------------------------------------------


class MemberEmailSent(BaseModel):
    """A `200`: the email went out, or dev email mode held it back."""

    #: `sent`, or `not_on_dev_list` when nothing was sent.
    code: str
    kind: Intent
    lifespan_seconds: int = Field(serialization_alias="lifespanSeconds")


@dataclass(frozen=True)
class _Outcome:
    status: int
    code: str
    #: The provisioning service's status, for the audit row. `None` when it did
    #: not answer.
    upstream: int | None
    body: dict[str, Any]
    headers: dict[str, str] | None = None


_STATUS_MESSAGES: dict[int, str] = {
    404: "The provisioning service could not find the member or their account.",
    409: "The provisioning service refused: the account's state does not allow this email.",
    429: "This account was emailed a few minutes ago; wait before sending again.",
    502: "A dependency behind the provisioning service failed; a retry may succeed.",
}


def _refused(intent: str, exc: Any) -> _Outcome:
    """Map a `ProvisioningApiError` onto this route's answer, by status and code."""
    status = int(exc.status_code or 502)
    code = exc.code

    if status in (401, 403):
        # This service's own credential was refused: a deployment fault, and never
        # the manager's refusal.
        logger.error(
            "The provisioning service refused %s with %s %s. Client %r needs the scope "
            "'provisioning.participants.write' and an audience mapper onto "
            "svc-provisioning: %s",
            intent,
            status,
            code,
            settings.oidc_client_id,
            exc,
        )
        return _Outcome(
            502,
            "provisioning_refused",
            status,
            {
                "code": "provisioning_refused",
                "message": "The provisioning service refused this service's credential.",
            },
        )

    # Any other status, known or not, passes through with its code unchanged.
    logger.info("Provisioning answered %s for %s: %s %s", intent, status, code, exc)
    code = code or f"http_{status}"
    body: dict[str, Any] = {
        "code": code,
        "message": _STATUS_MESSAGES.get(status, f"The provisioning service answered {status}."),
    }
    headers = None
    if status == 429 and exc.retry_after is not None:
        body["retryAfterSeconds"] = exc.retry_after
        headers = {"Retry-After": str(exc.retry_after)}
    return _Outcome(status, code, status, body, headers)


async def _send(delegation: Delegation, member_key: str, intent: str) -> _Outcome:
    from celine.sdk.provisioning import ProvisioningApiError

    try:
        answer = await provisioning.send_member_email(
            delegation.community, member_key, intent=intent
        )
    except ProvisioningApiError as exc:
        return _refused(intent, exc)
    except httpx.TransportError as exc:
        logger.warning("Provisioning service unreachable for %s: %s", intent, exc)
        return _Outcome(
            503,
            "provisioning_unavailable",
            None,
            {
                "code": "provisioning_unavailable",
                "message": "The provisioning service did not answer.",
            },
        )
    except (httpx.HTTPStatusError, ConfigurationError) as exc:
        # Only this service's own token request raises these: the realm refused
        # `OIDC_CLIENT_ID`, or `OIDC_BASE_URL` is missing. A deployment fault.
        logger.error("Could not authenticate to the provisioning service for %s: %s", intent, exc)
        return _Outcome(
            502,
            "provisioning_refused",
            None,
            {
                "code": "provisioning_refused",
                "message": "This service could not authenticate to the provisioning service.",
            },
        )

    # Plain enums in the generated schema: read the value, never compare the member.
    code = getattr(answer.invitation, "value", answer.invitation)
    sent = MemberEmailSent(code=code, kind=intent, lifespan_seconds=answer.lifespan)
    return _Outcome(200, code, 200, sent.model_dump(by_alias=True))


async def _email_member(
    delegation: Delegation,
    member_key: str,
    intent: str,
    db,
    ip: str,
) -> JSONResponse:
    outcome = await _send(delegation, member_key, intent)

    # Every authorised attempt is recorded, a refusal from the provisioning service
    # included. The dashboard keeps its own row for the same press.
    await audit_service.record_and_commit(
        db,
        action=AUDIT_ACTIONS[intent],
        entity_type="registry_member",
        entity_id=member_key,
        actor=Actor.delegated(delegation.operator, delegation.service),
        rec_slug=delegation.rec_slug,
        ip=ip,
        detail=(
            f"community={delegation.community} code={outcome.code} "
            f"status={outcome.upstream if outcome.upstream is not None else 'none'}"
        ),
    )

    content = outcome.body if outcome.status == 200 else {"detail": outcome.body}
    return JSONResponse(content, status_code=outcome.status, headers=outcome.headers)


_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "`invalid_token`, `actor_token_invalid`, `proxy_token_refused`"},
    403: {"description": "`forbidden`: the policy's reason is the message"},
    404: {
        "description": "`community_not_served`; or passed through: `community_not_found`, "
        "`member_not_found`, `account_not_found`"
    },
    409: {
        "description": "`community_ambiguous`; or passed through: `account_disabled`, "
        "`no_email`, `has_password` (invitation), `no_password` (password reset)"
    },
    429: {"description": "`cooldown`, with `retryAfterSeconds` and `Retry-After`"},
    502: {
        "description": "`provisioning_refused`; or passed through: `send_failed`, "
        "`registry_unavailable`, `provisioning_failed`"
    },
    503: {"description": "`provisioning_not_configured`, `provisioning_unavailable`"},
}


@router.post("/invitation", response_model=MemberEmailSent, responses=_RESPONSES)
async def send_member_invitation(
    member_key: MemberKey,
    delegation: DelegationDep,
    db: DbDep,
    ip: IpDep,
) -> JSONResponse:
    """Email the member an invitation to set their first password.

    Refused with `409 has_password` when the account already has one; the
    password-reset route is then the one to call.
    """
    return await _email_member(delegation, member_key, "invitation", db, ip)


@router.post("/password-reset", response_model=MemberEmailSent, responses=_RESPONSES)
async def send_member_password_reset(
    member_key: MemberKey,
    delegation: DelegationDep,
    db: DbDep,
    ip: IpDep,
) -> JSONResponse:
    """Email the member a link to reset their password.

    Refused with `409 no_password` when the account has none; the invitation route
    is then the one to call.
    """
    return await _email_member(delegation, member_key, "password_reset", db, ip)
