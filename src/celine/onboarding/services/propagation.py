"""Carrying a corrected value from onboarding to every system that holds a copy.

A revision recorded after approval (`services/revision.py`) leaves the old value in
the systems approval wrote it to. Each of those is a step here, recorded on its own
`SubmissionRevisionStep` row with its state, so a failure is visible on the
submission and retried on its own, like the enablement steps:

| Field | Steps, in order |
|---|---|
| first / last name | `account_profile` (Keycloak, through provisioning), `registry_name` |
| email | `account_profile`, `invitation`, `identity_mapping` |
| POD | `registry_delivery_point` (one write: add, remove the old, relink meters), `consent_keys` |

**A step sends what the submission holds now**, never the revision's own value: a
retry after a later correction must not put the older value back. A later revision
of the same field supersedes an earlier one's unfinished steps (`skipped`).

**Order within a field.** `invitation` and `identity_mapping` wait for
`account_profile`: until the account carries the new address, the member still
signs in with the old one, and a mapping already moved to the new address would
lose them their Data sharing page.

**No value is written to a step row or to the log**: names, addresses and
usernames stay in the revision row, encrypted. Errors are recorded as a code and
a fixed sentence.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.models.revision import SubmissionRevision, SubmissionRevisionStep
from celine.onboarding.models.submission import Submission, SubmissionStatus
from celine.onboarding.services import enablement
from celine.onboarding.services.errors import ConfigurationError
from celine.onboarding.services.revision import (
    PROPAGATION,
    SKIP_ENABLEMENT_REVOKED,
    PropagationStep,
    RevisableField,
    RevisionOutcome,
)

logger = logging.getLogger(__name__)


class StepStatus(enum.StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


#: Steps that run only once another step of the same revision is `done` or
#: `skipped`. See the module docstring.
WAITS_FOR: dict[PropagationStep, PropagationStep] = {
    PropagationStep.INVITATION: PropagationStep.ACCOUNT_PROFILE,
    PropagationStep.IDENTITY_MAPPING: PropagationStep.ACCOUNT_PROFILE,
    # The holder must be sent the POD the registry now holds, and
    # `subject_supply_keys` reads it there.
    PropagationStep.CONSENT_KEYS: PropagationStep.REGISTRY_DELIVERY_POINT,
}

#: What the console shows for a provisioning refusal, by its code. Fixed text: the
#: service's own message can quote the address and is only logged.
PROVISIONING_REASONS: dict[str, str] = {
    "email_taken": "Another account already uses this email address. Check the address "
    "with the member; nothing was changed.",
    "account_disabled": "The member's account is disabled, so it was not changed.",
    "account_not_found": "The member has no account to correct.",
    "member_not_found": "The registry holds no active member for this submission.",
    "community_not_found": "The registry holds no community for this REC.",
    "send_failed": "Keycloak could not send the email; the account was left as it was. Retry.",
    "registry_unavailable": "The provisioning service could not reach the REC registry. Retry.",
    "provisioning_failed": "Keycloak failed behind the provisioning service. Retry.",
    "cooldown": "The member was emailed a few minutes ago. Retry later.",
    "no_email": "The member's account has no email address.",
}

#: R16, word for word: the POD is another active member's.
HELD_REASON = "This POD is already recorded for another member — check it with the member."


@dataclass
class StepResult:
    status: StepStatus
    reason: str | None = None
    outcome: str | None = None
    error_code: str | None = None


@dataclass
class RunContext:
    submission: Submission
    revision: SubmissionRevision
    rows: dict[str, SubmissionRevisionStep]
    #: The Keycloak uuid approval's login step recorded. Not personal data.
    keycloak_user_id: str | None = None
    #: The account's username as the provisioning service read it back, when a
    #: call in this run returned it. Kept in memory only, never on a row.
    username: str | None = field(default=None)


class StepError(Exception):
    """A step's refusal, already in words for the row."""

    def __init__(self, error_code: str, reason: str) -> None:
        super().__init__(f"{error_code}: {reason}")
        self.error_code = error_code
        self.reason = reason


# ── the steps ────────────────────────────────────────────────────────────────


def _provisioning_failure(exc: Exception) -> StepError:
    """A `ProvisioningApiError` or a transport failure as a code and a sentence."""
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    logger.warning("Provisioning refused a correction (%s %s)", status, code)
    if code in PROVISIONING_REASONS:
        return StepError(code, PROVISIONING_REASONS[code])
    if status in (401, 403):
        return StepError(
            f"http_{status}",
            "The provisioning service refused this service's credential. A platform "
            "operator has to fix it; the details are in the server log.",
        )
    if status == 405:
        return StepError(
            "http_405",
            "The provisioning service is older than API 1.4.0 and cannot correct an "
            "account. A platform operator has to upgrade it.",
        )
    if status is None:
        return StepError("unreachable", "The provisioning service could not be reached. Retry.")
    return StepError(f"http_{status}", f"The provisioning service answered {status}. Retry.")


async def _account_profile(ctx: RunContext) -> StepResult:
    from celine.sdk.provisioning import ProvisioningApiError

    from celine.onboarding.services import provisioning

    field_ = RevisableField(ctx.revision.field)
    value = getattr(ctx.submission, field_.value)
    if field_ is RevisableField.EMAIL:
        # The form approval provisioned the account with.
        value = (value or "").strip().lower()
    try:
        answer = await provisioning.update_account(ctx.submission, **{field_.value: value})
    except ProvisioningApiError as exc:
        raise _provisioning_failure(exc) from exc
    except httpx.HTTPError as exc:
        raise _provisioning_failure(exc) from exc
    if answer is None:
        return StepResult(StepStatus.SKIPPED, reason="This member has no login to correct.")

    ctx.username = answer.username
    changed = [getattr(c, "value", c) for c in (answer.changed or [])]
    verification = getattr(answer.verification, "value", answer.verification)
    if not changed:
        return StepResult(
            StepStatus.DONE,
            reason="The account already held the corrected value.",
            outcome=verification,
        )
    if field_ is RevisableField.EMAIL:
        sent = {
            "sent": "a verification link was sent to the new address",
            "not_on_dev_list": "no link was sent (address not on this deployment's dev list)",
        }.get(verification, "no verification link was sent")
        return StepResult(
            StepStatus.DONE, reason=f"Account address corrected; {sent}.", outcome=verification
        )
    return StepResult(StepStatus.DONE, reason="Account name corrected.", outcome=verification)


async def _invitation(ctx: RunContext) -> StepResult:
    """Invite again, to the new address, a member who never set a password.

    The provisioning service decides, in the same call that sends: an account
    with a password is refused `has_password`, which is the ordinary case and
    means the verification link already sent is all the member needs.
    """
    from celine.onboarding.services import provisioning

    profile = ctx.rows.get(PropagationStep.ACCOUNT_PROFILE)
    if profile is not None and profile.status == StepStatus.SKIPPED:
        return StepResult(StepStatus.SKIPPED, reason="This member has no login to invite.")
    if profile is not None and profile.outcome == "not_requested":
        return StepResult(
            StepStatus.SKIPPED,
            reason="The account's address did not change, so nothing is sent again.",
        )

    try:
        code = await provisioning.invite_participant(ctx.submission)
    except ConfigurationError as exc:
        logger.error("Could not re-send the invitation for %s: %s", ctx.submission.ref, exc)
        raise StepError(
            "configuration",
            "The invitation could not be sent: this deployment is misconfigured. The "
            "details are in the server log.",
        ) from exc
    except Exception as exc:
        raise _provisioning_failure(exc) from exc

    if code == "has_password":
        return StepResult(
            StepStatus.SKIPPED,
            reason="The member has already set a password; the verification link is enough.",
            outcome=code,
        )
    if code == "sent":
        return StepResult(
            StepStatus.DONE,
            reason="The invitation to set a password was sent to the new address.",
            outcome=code,
        )
    if code == "not_on_dev_list":
        return StepResult(
            StepStatus.DONE,
            reason="No invitation was sent: the address is not on this deployment's dev list.",
            outcome=code,
        )
    raise StepError(code, PROVISIONING_REASONS.get(code, f"The invitation was refused ({code})."))


async def _identity_mapping(ctx: RunContext) -> StepResult:
    """Re-sync the identity registry's mapping: the same DID, the new address.

    The member's Data sharing page resolves them by their token's email at the
    identity registry, so this is what keeps them reaching it. The username goes
    too when it is known, because the registry keeps the one it holds only when
    none is sent, and the data plane joins on it.
    """
    from celine.onboarding.config.settings import settings
    from celine.onboarding.services import dataspace_identity, provisioning, template_service

    submission = ctx.submission
    if not settings.dataspace_enabled:
        return StepResult(StepStatus.SKIPPED, reason="This deployment is not in a dataspace.")
    await template_service.ensure_fresh()
    if not template_service.dataspace_binding(submission.rec_slug).enabled:
        return StepResult(StepStatus.SKIPPED, reason="This community is not in a dataspace.")
    if not submission.dataspace_did:
        return StepResult(StepStatus.SKIPPED, reason="This member has no dataspace identity.")
    if not ctx.keycloak_user_id:
        # Approval writes the mapping only for a member with a login, so there is
        # none to move.
        return StepResult(
            StepStatus.SKIPPED, reason="This member has no login, so no mapping to update."
        )

    email = (submission.email or "").strip().lower()
    if ctx.username is None:
        # A retry of this step alone: read the username back with a correction
        # that changes nothing (the account already holds this address, so the
        # service writes and sends nothing).
        try:
            answer = await provisioning.update_account(submission, email=email)
        except Exception as exc:
            raise _provisioning_failure(exc) from exc
        ctx.username = answer.username if answer is not None else None

    access = await dataspace_identity.registry_access()
    try:
        await dataspace_identity.sync_keycloak_mapping(
            access.base_url,
            access.headers,
            did=submission.dataspace_did,
            keycloak_user_id=ctx.keycloak_user_id,
            keycloak_realm=provisioning.keycloak_realm(),
            email=email,
            username=ctx.username,
        )
    except dataspace_identity.KeycloakSyncError as exc:
        if exc.status_code == 409:
            raise StepError(
                "mapping_conflict",
                "The identity registry binds this member's account to another DID. An "
                "operator has to resolve it there; retrying will not.",
            ) from exc
        if exc.status_code is None:
            raise StepError(
                "unreachable", "The identity registry could not be reached. Retry."
            ) from exc
        raise StepError(
            f"http_{exc.status_code}",
            f"The identity registry answered {exc.status_code}. Retry.",
        ) from exc
    return StepResult(
        StepStatus.DONE, reason="The dataspace identity now maps to the new address, same DID."
    )


async def _registry_name(ctx: RunContext) -> StepResult:
    from celine.onboarding.services import rec_registry

    submission = ctx.submission
    try:
        detail = await rec_registry.set_member_name(
            submission.rec_slug,
            member_key=submission.ref,
            name=rec_registry.member_name(submission),
        )
    except rec_registry.RegistryRefusalError as exc:
        raise StepError(
            f"http_{exc.status_code}", f"The REC registry answered {exc.status_code}. Retry."
        ) from exc
    except httpx.HTTPError as exc:
        raise StepError("unreachable", "The REC registry could not be reached. Retry.") from exc
    if detail.startswith("no registry") or detail.startswith("this community"):
        return StepResult(StepStatus.SKIPPED, reason=f"Not sent: {detail}.")
    return StepResult(StepStatus.DONE, reason="The registry member holds the corrected name.")


def registry_pod(submission: Submission, revision: SubmissionRevision) -> str | None:
    """The POD the registry holds for this member before ``revision`` lands.

    Approval wrote the column as it was then, which is the previous value of the
    first revision made after approval (the first with steps). Each later
    revision whose registry step is ``done`` moved it on. A revision that never
    reached the registry (failed, superseded) did not, so the next one replaces
    what is really there, not what that one meant to write.
    """
    held: str | None = None
    seen = False
    for row in submission.revisions:
        if row.field != RevisableField.POD_CODE.value or not row.steps:
            continue
        if row is revision:
            break
        if not seen:
            held, seen = row.previous_value, True
        step = next(
            (s for s in row.steps if s.step == PropagationStep.REGISTRY_DELIVERY_POINT), None
        )
        if step is not None and step.status == StepStatus.DONE:
            held = row.new_value
    return held if seen else revision.previous_value


async def _registry_delivery_point(ctx: RunContext) -> StepResult:
    from celine.sdk.rec_registry import RecRegistryApiError

    from celine.onboarding.services import rec_registry

    submission = ctx.submission
    pod = (submission.pod_code or "").strip().upper()
    old = (registry_pod(submission, ctx.revision) or "").strip().upper() or None
    replaces = old if old and old != pod else None
    try:
        detail = await rec_registry.replace_delivery_point(
            submission.rec_slug, member_key=submission.ref, pod=pod, replaces=replaces
        )
    except RecRegistryApiError as exc:
        # The text can name another member or the POD: code and status only.
        logger.warning(
            "REC registry refused the corrected supply point of %s (%s %s)",
            submission.ref,
            exc.status_code,
            exc.code,
        )
        if exc.code == "delivery_point_held":
            raise StepError("delivery_point_held", HELD_REASON) from exc
        if exc.status_code == 404 and exc.code is None:
            raise StepError(
                "previous_pod_not_found",
                "The registry no longer holds the previous POD for this member, so it "
                "cannot be replaced. Check the member in the registry.",
            ) from exc
        if exc.code in ("member_not_found", "community_not_found"):
            raise StepError(exc.code, "The registry holds no such member for this submission.")
        status = exc.status_code
        raise StepError(
            f"http_{status}" if status else "error",
            f"The REC registry answered {status}. Retry."
            if status
            else "The REC registry failed. Retry.",
        ) from exc
    except httpx.HTTPError as exc:
        raise StepError("unreachable", "The REC registry could not be reached. Retry.") from exc
    if detail.startswith("no registry") or detail.startswith("this community"):
        return StepResult(StepStatus.SKIPPED, reason=f"Not sent: {detail}.")
    return StepResult(
        StepStatus.DONE,
        reason="The registry member holds the corrected POD"
        + ("; the previous one was removed and its meters relinked." if replaces else "."),
    )


async def _consent_keys(ctx: RunContext) -> StepResult:
    """Re-send the member's grants at every holder with the corrected POD."""
    from celine.onboarding.config.settings import settings
    from celine.onboarding.services import dataspace_identity

    submission = ctx.submission
    if not settings.dataspace_enabled or not settings.ds_connector_url:
        return StepResult(StepStatus.SKIPPED, reason="This deployment is not in a dataspace.")
    if not submission.dataspace_did:
        return StepResult(StepStatus.SKIPPED, reason="This member has no dataspace identity.")

    report: list[str] = []
    try:
        await dataspace_identity.refresh_keys(submission, report=report)
    except ValueError as exc:
        # Names offers and connectors, and may quote a connector's answer.
        logger.warning("Key refresh for %s did not complete: %s", submission.ref, exc)
        written = f" {len(report)} grant(s) were re-sent before it." if report else ""
        raise StepError(
            "consent_refused",
            "A connector did not take every grant with the corrected POD; details in "
            f"the server log. Retry.{written}",
        ) from exc
    if not report:
        return StepResult(
            StepStatus.DONE,
            reason="The member holds no grant at another participant; nothing to re-send.",
            outcome="resent:0",
        )
    return StepResult(
        StepStatus.DONE,
        reason="Re-sent with the corrected POD: " + "; ".join(report) + ".",
        outcome=f"resent:{len(report)}",
    )


# The runner for each step.
RUNNERS: dict[PropagationStep, Callable[[RunContext], Awaitable[StepResult]]] = {
    PropagationStep.ACCOUNT_PROFILE: _account_profile,
    PropagationStep.INVITATION: _invitation,
    PropagationStep.IDENTITY_MAPPING: _identity_mapping,
    PropagationStep.REGISTRY_NAME: _registry_name,
    PropagationStep.REGISTRY_DELIVERY_POINT: _registry_delivery_point,
    PropagationStep.CONSENT_KEYS: _consent_keys,
}


# ── running ──────────────────────────────────────────────────────────────────


def _blocked_by(ctx: RunContext, step: PropagationStep) -> SubmissionRevisionStep | None:
    waited = WAITS_FOR.get(step)
    row = ctx.rows.get(waited) if waited else None
    if row is not None and row.status not in (StepStatus.DONE, StepStatus.SKIPPED):
        return row
    return None


async def _run_one(db: AsyncSession, ctx: RunContext, step: PropagationStep) -> None:
    row = ctx.rows[step]
    runner = RUNNERS[step]
    blocker = _blocked_by(ctx, step)
    if blocker is not None:
        row.status = StepStatus.PENDING
        row.reason = f"Waits for {blocker.step}."
        return

    row.attempts = (row.attempts or 0) + 1
    row.started_at = datetime.now(UTC)
    try:
        result = await runner(ctx)
    except StepError as exc:
        result = StepResult(StepStatus.FAILED, reason=exc.reason, error_code=exc.error_code)
    except ConfigurationError as exc:
        logger.error("Propagation step %s is misconfigured: %s", step.value, exc)
        result = StepResult(
            StepStatus.FAILED,
            reason="This step is not configured in this deployment. A platform operator "
            "has to fix it; the details are in the server log.",
            error_code="configuration",
        )
    except Exception as exc:
        # The exception text can quote what a service echoed back, so it goes to
        # the log and only its type to the row.
        logger.warning("Propagation step %s failed for %s: %s", step.value, ctx.submission.ref, exc)
        result = StepResult(
            StepStatus.FAILED,
            reason=f"Unexpected failure ({type(exc).__name__}). Retry; details in the log.",
            error_code="error",
        )

    row.status = result.status
    row.reason = result.reason
    row.outcome = result.outcome
    row.error_code = result.error_code if result.status == StepStatus.FAILED else None
    row.completed_at = datetime.now(UTC)
    # Committed per step, as enablement does: what one step did is recorded even
    # if the next one takes the process down.
    await db.commit()


def _ensure_rows(revision: SubmissionRevision, steps) -> dict[str, SubmissionRevisionStep]:
    rows = {row.step: row for row in revision.steps}
    for position, step in enumerate(steps):
        if step.value not in rows:
            row = SubmissionRevisionStep(
                step=step.value,
                position=position,
                status=StepStatus.PENDING,
                attempts=0,
                created_at=datetime.now(UTC),
            )
            revision.steps.append(row)
            rows[step.value] = row
    return rows


async def _keycloak_user_id(db: AsyncSession, submission: Submission) -> str | None:
    """The Keycloak uuid approval's login step recorded, if it has one."""
    rows = await enablement.load_steps(db, submission.id)
    login = rows.get(enablement.EnablementStep.KEYCLOAK_USER)
    return login.external_ref if login is not None else None


def _supersede(submission: Submission, revision: SubmissionRevision) -> None:
    """A newer revision of the same field ends an older one's unfinished steps."""
    for older in submission.revisions:
        if older is revision or older.field != revision.field:
            continue
        for row in older.steps:
            if row.status in (StepStatus.PENDING, StepStatus.FAILED):
                row.status = StepStatus.SKIPPED
                row.error_code = None
                row.reason = f"Superseded by revision {revision.id}."


async def start(db: AsyncSession, outcome: RevisionOutcome) -> list[SubmissionRevisionStep]:
    """Record a fresh revision's steps and run them. Nothing before approval.

    Never raises for a step: each failure is on its row, and the revision itself
    is already committed.
    """
    if not outcome.propagation:
        return []
    revision, submission = outcome.revision, outcome.submission
    rows = _ensure_rows(revision, outcome.propagation)
    _supersede(submission, revision)

    if outcome.skip_reason:
        for row in rows.values():
            row.status = StepStatus.SKIPPED
            row.reason = f"Not propagated: {outcome.skip_reason}."
            row.completed_at = datetime.now(UTC)
        await db.commit()
        return list(revision.steps)

    await db.commit()
    return await run(db, submission, revision)


async def run(
    db: AsyncSession,
    submission: Submission,
    revision: SubmissionRevision,
    *,
    only: PropagationStep | None = None,
) -> list[SubmissionRevisionStep]:
    """Run a revision's unfinished steps, in order; or one named step.

    `pending` and `failed` steps run; `done` and `skipped` are left alone, except
    that a step named in `only` runs whatever its state, as a deliberate
    operator act, and the steps still waiting for it run after it. A member
    whose enablement was revoked since gets every unfinished step `skipped`, as
    at recording (R14).
    """
    steps = PROPAGATION[RevisableField(revision.field)]
    rows = _ensure_rows(revision, steps)

    if submission.status != SubmissionStatus.APPROVED or enablement.revoked(
        await enablement.load_steps(db, submission.id)
    ):
        for row in rows.values():
            if row.status in (StepStatus.PENDING, StepStatus.FAILED):
                row.status = StepStatus.SKIPPED
                row.error_code = None
                row.reason = f"Not propagated: {SKIP_ENABLEMENT_REVOKED}."
        await db.commit()
        return list(revision.steps)

    ctx = RunContext(
        submission=submission,
        revision=revision,
        rows=rows,
        keycloak_user_id=await _keycloak_user_id(db, submission),
    )
    for step in steps:
        row = rows[step.value]
        if only is not None and step is not only:
            # A named retry also releases what was waiting for that step.
            if not (WAITS_FOR.get(step) is only and row.status == StepStatus.PENDING):
                continue
        elif only is None and row.status in (StepStatus.DONE, StepStatus.SKIPPED):
            continue
        await _run_one(db, ctx, step)
    await db.commit()
    return list(revision.steps)
