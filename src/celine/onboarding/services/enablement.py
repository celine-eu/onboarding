"""Running, recording and repairing what approval does.

Approving somebody enables them, which means four things landing in this order:

| # | Step | Where | On failure |
|---|---|---|---|
| 1 | Login identity | Keycloak user | **closed** |
| 2 | Community member | rec-registry | **closed** |
| 3 | Dataspace identity | identity registry | **closed** |
| 4 | Standing sharing consent | connector | soft |

The order is load-bearing. The registry keys a member on `(community, user_id)`,
so the Keycloak user has to exist first; the dataspace identity is later because
it is the step that can be retried afterwards.

Step 3 does one more thing than its name says: it writes the DID it mints back
onto the member step 2 created. That is deliberate rather than untidy — the DID
does not exist any earlier, and it is the key anything else uses to attribute a
dataspace consent to a member. A member without one is invisible to every
consent-driven export, so it fails closed with the rest of the step.

"Fails closed" means the submission does **not** become approved. What changed is
that the failure is now durable: the step row records which step, why, and how
many times, so the remedy is retrying that step rather than pressing Approve again
and re-running all four.

One consequence worth stating. When a closed step fails, the *successful* steps
before it are committed rather than rolled back. They really happened — a
credential was issued, a member was created — and forgetting them locally would
orphan them remotely, so the next attempt would mint a second one. Recording them
is what makes the retry idempotent.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.models.enablement import (
    EnablementStatus,
    EnablementStep,
    SubmissionEnablementStep,
)
from celine.onboarding.models.submission import Submission

logger = logging.getLogger(__name__)


class EnablementError(RuntimeError):
    """A fail-closed step did not succeed, so the person is not enabled."""

    def __init__(self, step: EnablementStep, message: str) -> None:
        super().__init__(message)
        self.step = step
        self.message = message


@dataclass
class StepOutcome:
    status: EnablementStatus
    external_ref: str | None = None
    detail: str | None = None


@dataclass
class RunContext:
    """Carries results between steps.

    Step 3 needs the Keycloak user id step 1 produced. Reading it from the step
    rows rather than threading it through arguments means a *retry* of step 3
    alone still finds it.

    The Keycloak *username* is different: it is not on the step row, which holds
    one external reference and that one is the user id. It is set here when step 1
    runs, and is therefore ``None`` on a retry of step 2 alone — which is why
    :func:`~celine.onboarding.services.rec_registry.member_user_id` falls back to
    the submission's email rather than requiring it.
    """

    submission: Submission
    rows: dict[str, SubmissionEnablementStep] = field(default_factory=dict)
    keycloak_username: str | None = None

    @property
    def keycloak_user_id(self) -> str | None:
        row = self.rows.get(EnablementStep.KEYCLOAK_USER)
        if row is not None and row.status == EnablementStatus.SUCCEEDED:
            return row.external_ref
        return None

    @property
    def registry_member_key(self) -> str | None:
        """The member step 2 created, for step 3 to write the DID onto.

        Read from the step row for the same reason as
        :attr:`keycloak_user_id`: a *retry of step 3 alone* has to find it, and
        it is on the row rather than threaded through arguments. ``None`` when
        step 2 was skipped — this community declares no registry — so there is
        no member to write to.
        """
        row = self.rows.get(EnablementStep.REC_REGISTRY_MEMBER)
        if row is not None and row.status == EnablementStatus.SUCCEEDED:
            return row.external_ref
        return None


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------


async def _run_keycloak_user(ctx: RunContext) -> StepOutcome:
    from celine.onboarding.services.keycloak_identity import provision_keycloak_user

    result = await provision_keycloak_user(ctx.submission)
    if result is None:
        return StepOutcome(EnablementStatus.SKIPPED, detail="Keycloak provisioning is disabled")
    # Step 2 registers this as the member's `user_id`, because it is what their
    # token will carry. Read back from Keycloak rather than assumed: a user that
    # already existed may authenticate under a username that is not their email.
    ctx.keycloak_username = result.username
    return StepOutcome(
        EnablementStatus.SUCCEEDED,
        external_ref=result.user_id,
        detail="created" if result.created else "already existed",
    )


async def _revoke_keycloak_user(ctx: RunContext, row: SubmissionEnablementStep) -> str:
    from celine.onboarding.services.keycloak_identity import disable_keycloak_user

    if not row.external_ref:
        return "no Keycloak user recorded"
    await disable_keycloak_user(row.external_ref)
    return f"disabled Keycloak user {row.external_ref}"


async def _run_registry_member(ctx: RunContext) -> StepOutcome:
    from celine.onboarding.services.rec_registry import register_member

    key = await register_member(ctx.submission, keycloak_username=ctx.keycloak_username)
    if key is None:
        return StepOutcome(
            EnablementStatus.SKIPPED,
            detail="this community declares no rec_registry binding",
        )
    return StepOutcome(EnablementStatus.SUCCEEDED, external_ref=key)


async def _revoke_registry_member(ctx: RunContext, row: SubmissionEnablementStep) -> str:
    from celine.onboarding.services.rec_registry import deactivate_member

    if not row.external_ref:
        return "no registry member recorded"
    await deactivate_member(ctx.submission, member_key=row.external_ref)
    return f"deactivated registry member {row.external_ref}"


async def _run_dataspace_identity(ctx: RunContext) -> StepOutcome:
    from celine.onboarding.config.settings import settings
    from celine.onboarding.services.dataspace_identity import provision_user_identity

    await provision_user_identity(
        ctx.submission,
        keycloak_user_id=ctx.keycloak_user_id,
        keycloak_realm=(settings.dataspace_keycloak_realm if ctx.keycloak_user_id else None),
        # The consent share is step 4, with its own row and its own retry. Fusing
        # them meant a soft failure was invisible inside a hard one.
        provision_shares=False,
    )
    if not ctx.submission.dataspace_vc_id:
        return StepOutcome(
            EnablementStatus.SKIPPED,
            detail="this community declares no dataspace binding",
        )

    # The DID this step just minted goes onto the registry member, which is the
    # only place anything can join a dataspace consent back to the person who
    # gave it: the connector answers *who consents* in DIDs, and the registry
    # knows *what they hold*. It belongs to this step rather than to step 2
    # because the DID does not exist until this step runs.
    #
    # Inside the step, not beside it, and that is deliberate. A member left
    # without a DID is invisible to every consent-driven export — the same class
    # of failure as a member who does not exist, which is why step 2 fails
    # closed too. It is also why the failure must not be swallowed: the export
    # would silently omit them and read as complete.
    detail = await _write_member_did(ctx)

    return StepOutcome(
        EnablementStatus.SUCCEEDED,
        external_ref=ctx.submission.dataspace_vc_id,
        detail=detail,
    )


async def _write_member_did(ctx: RunContext) -> str | None:
    """Put the minted DID on the registry member, if there is one."""
    from celine.onboarding.services.rec_registry import set_member_did

    member_key = ctx.registry_member_key
    if member_key is None:
        return None
    if not ctx.submission.dataspace_did:
        return None

    return await set_member_did(
        ctx.submission,
        member_key=member_key,
        did=ctx.submission.dataspace_did,
    )


async def _revoke_dataspace_identity(ctx: RunContext, row: SubmissionEnablementStep) -> str:
    from celine.onboarding.services.dataspace_identity import revoke_user_identity

    return await revoke_user_identity(ctx.submission)


async def _run_dataspace_share(ctx: RunContext) -> StepOutcome:
    from celine.onboarding.config.settings import settings
    from celine.onboarding.services.dataspace_identity import provision_user_shares

    if not ctx.submission.data_sharing_consent:
        return StepOutcome(EnablementStatus.SKIPPED, detail="no data-sharing consent was given")
    if not settings.ds_connector_url:
        return StepOutcome(EnablementStatus.SKIPPED, detail="no dataspace connector is configured")

    await provision_user_shares(ctx.submission, raise_on_error=True)
    return StepOutcome(EnablementStatus.SUCCEEDED)


async def _revoke_dataspace_share(ctx: RunContext, row: SubmissionEnablementStep) -> str:
    from celine.onboarding.config.settings import settings
    from celine.onboarding.services.dataspace_identity import withdraw_user_shares

    if not settings.ds_connector_url:
        return "no dataspace connector is configured"
    if not ctx.submission.data_sharing_consent:
        return "no data-sharing consent was recorded"

    ok = await withdraw_user_shares(ctx.submission)
    return "standing consent withdrawn" if ok else "nothing to withdraw"


@dataclass(frozen=True)
class StepSpec:
    step: EnablementStep
    label: str
    # Whether a failure blocks approval. A missing consent row is recoverable and
    # has a retry; a participant missing from the registry is enabled in name
    # only — invisible to every pipeline, dashboard and digital-twin query, all of
    # which join on the registry. That is not a state anything can work around.
    fail_closed: bool
    run: Callable[[RunContext], Awaitable[StepOutcome]]
    revoke: Callable[[RunContext, SubmissionEnablementStep], Awaitable[str]] | None


PIPELINE: tuple[StepSpec, ...] = (
    StepSpec(
        EnablementStep.KEYCLOAK_USER,
        "Login identity",
        fail_closed=True,
        run=_run_keycloak_user,
        revoke=_revoke_keycloak_user,
    ),
    StepSpec(
        EnablementStep.REC_REGISTRY_MEMBER,
        "Community member",
        fail_closed=True,
        run=_run_registry_member,
        revoke=_revoke_registry_member,
    ),
    StepSpec(
        EnablementStep.DATASPACE_IDENTITY,
        "Dataspace identity",
        fail_closed=True,
        run=_run_dataspace_identity,
        revoke=_revoke_dataspace_identity,
    ),
    StepSpec(
        EnablementStep.DATASPACE_SHARE,
        "Standing sharing consent",
        fail_closed=False,
        run=_run_dataspace_share,
        # This step grants on the person's behalf, so it withdraws on their
        # behalf too. An earlier version left this `None`, reasoning that
        # withdrawal is the subject's own act and belongs in the participant
        # webapp. That is true of a person *choosing* to stop sharing, and it is
        # not what happens here: a revocation removes them from the community,
        # and the same sequence deletes the credential the webapp would have
        # authenticated that choice with. The consent stood, and its subject had
        # no way left to withdraw it.
        #
        # Nothing in ds distinguishes the two: `POST /consent/admin/shares`
        # with `enabled: false` and `POST /consent/my/{id}/revoke` move the same
        # row to the same `revoked` status. They differ in which credential opens
        # the door, not in what is written — so `revocation_reason` is where this
        # path says why, and the person's own withdrawal in the webapp is
        # untouched and still theirs.
        revoke=_revoke_dataspace_share,
    ),
)

SPECS: dict[str, StepSpec] = {spec.step: spec for spec in PIPELINE}


def spec_for(step: str) -> StepSpec:
    try:
        return SPECS[EnablementStep(step)]
    except ValueError:
        raise KeyError(f"Unknown enablement step: {step}") from None


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


async def load_steps(db: AsyncSession, submission_id) -> dict[str, SubmissionEnablementStep]:
    result = await db.execute(
        select(SubmissionEnablementStep).where(
            SubmissionEnablementStep.submission_id == submission_id
        )
    )
    return {row.step: row for row in result.scalars().all()}


async def ensure_rows(
    db: AsyncSession, submission: Submission
) -> dict[str, SubmissionEnablementStep]:
    """One row per step, created on demand. Idempotent."""
    rows = await load_steps(db, submission.id)
    for spec in PIPELINE:
        if spec.step not in rows:
            # `attempts` is set explicitly rather than left to the column
            # default, which only materialises at flush: the runner increments it
            # immediately, and a freshly constructed row would still be None.
            row = SubmissionEnablementStep(
                submission_id=submission.id,
                step=spec.step,
                status=EnablementStatus.PENDING,
                attempts=0,
            )
            db.add(row)
            rows[spec.step] = row
    await db.flush()
    return rows


def state_of(rows: dict[str, SubmissionEnablementStep]) -> str:
    """A one-word summary for a queue column.

    `failed` wins over `partial`: an operator scanning a list needs to see the
    thing that needs them, not the average.
    """
    statuses = {row.status for row in rows.values()}
    if not statuses or statuses <= {EnablementStatus.PENDING}:
        return "not_started"
    if EnablementStatus.FAILED in statuses:
        return "failed"
    if statuses <= {EnablementStatus.SUCCEEDED, EnablementStatus.SKIPPED}:
        return "complete"
    return "partial"


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


async def _run_one(db: AsyncSession, ctx: RunContext, spec: StepSpec) -> SubmissionEnablementStep:
    row = ctx.rows[spec.step]
    row.status = EnablementStatus.RUNNING
    row.attempts += 1
    row.started_at = datetime.now(UTC)
    # Committed before the call so the attempt is counted even if the process
    # dies mid-request — the same reason the OTP verifier commits its counter.
    await db.commit()

    try:
        outcome = await spec.run(ctx)
    except Exception as exc:
        row.status = EnablementStatus.FAILED
        row.last_error = f"{type(exc).__name__}: {exc}"[:4000]
        row.completed_at = datetime.now(UTC)
        logger.warning("Enablement step %s failed for %s: %s", spec.step, ctx.submission.ref, exc)
        await db.commit()
        return row

    row.status = outcome.status
    row.external_ref = outcome.external_ref or row.external_ref
    row.detail = outcome.detail
    row.last_error = None
    row.completed_at = datetime.now(UTC)
    await db.commit()
    return row


async def enable(
    db: AsyncSession, submission: Submission, *, only: str | None = None
) -> dict[str, SubmissionEnablementStep]:
    """Run the pipeline, or one step of it.

    Raises `EnablementError` when a fail-closed step does not succeed, having
    first committed everything that did. A step already `succeeded` or `skipped`
    is not re-run: retry means "finish what is unfinished", not "do it all again".
    """
    rows = await ensure_rows(db, submission)
    ctx = RunContext(submission=submission, rows=rows)

    for spec in PIPELINE:
        if only is not None and spec.step != only:
            continue

        row = rows[spec.step]
        if row.status in (EnablementStatus.SUCCEEDED, EnablementStatus.SKIPPED):
            continue

        row = await _run_one(db, ctx, spec)
        if row.status == EnablementStatus.FAILED and spec.fail_closed:
            raise EnablementError(
                EnablementStep(spec.step),
                f"{spec.label} could not be provisioned: {row.last_error}",
            )

    return rows


async def retry(
    db: AsyncSession, submission: Submission, *, step: str | None = None
) -> dict[str, SubmissionEnablementStep]:
    """Re-run the failed steps, or one named step.

    Unlike `enable` this never raises on a fail-closed failure: the submission is
    already approved, so there is no decision to block — the operator asked to
    repair, and the answer is the step rows.
    """
    if step is not None:
        spec_for(step)  # raises KeyError on an unknown step

    try:
        return await enable(db, submission, only=step)
    except EnablementError:
        return await load_steps(db, submission.id)


async def revoke(db: AsyncSession, submission: Submission) -> dict[str, SubmissionEnablementStep]:
    """Undo enablement, in reverse order.

    Best-effort per step and recorded per step: a revocation that fails half way
    must leave a record of what is still out there, because that record is the
    only way anybody finds the rest.
    """
    rows = await ensure_rows(db, submission)
    ctx = RunContext(submission=submission, rows=rows)

    for spec in reversed(PIPELINE):
        row = rows[spec.step]
        if row.status != EnablementStatus.SUCCEEDED:
            continue
        if spec.revoke is None:
            logger.info("Step %s has no revocation path; leaving it in place", spec.step)
            continue

        try:
            detail = await spec.revoke(ctx, row)
        except Exception as exc:
            row.status = EnablementStatus.FAILED
            row.last_error = f"revoke failed — {type(exc).__name__}: {exc}"[:4000]
            logger.warning("Revoking %s failed for %s: %s", spec.step, submission.ref, exc)
            await db.commit()
            continue

        row.status = EnablementStatus.PENDING
        row.detail = detail
        row.last_error = None
        row.external_ref = None
        row.completed_at = datetime.now(UTC)
        await db.commit()

    return rows
