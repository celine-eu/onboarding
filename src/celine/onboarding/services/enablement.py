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

**Undoing them is not that order reversed**, and the exception is step 1 — see
`REVOKE_ORDER`. Revoking a login resolves the member through the registry
export, so it has to happen before the member is deactivated rather than after.

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

That is why step 1 creates the login **without** inviting anybody to it. The
invitation to set a password is sent after the fail-closed steps have all
succeeded, and recorded on step 1's row: an approval that fails leaves an account
with no credential and no email about it (celine-eu/onboarding#8). The account
itself cannot be disabled at that point — revocation resolves the member through
the registry, and step 2 is the step that may have failed.
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
from celine.onboarding.services.errors import ConfigurationError

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
    #: Step 1 only: the provisioning service's invitation reason code. A code
    #: beside `detail` because the console translates it and `detail` is English.
    invitation: str | None = None


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
    #: The step an operator named in a retry, if any. Step 4 reads it: named, it
    #: re-examines a member who declined on the form instead of skipping them.
    named: str | None = None

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


#: What each invitation reason code reads as in a step row's `detail`, which is
#: the English sentence the CLI and the log show. The console does not show it:
#: it translates the code itself (`admin.invitation.<code>`).
INVITATION_DETAIL: dict[str, str] = {
    "sent": "invitation sent",
    "has_password": "has a password",
    "not_on_dev_list": "invitation not sent (provisioning email mode)",
    "account_disabled": "invitation not sent: account is disabled",
    "not_requested": "invitation not sent yet: it goes out when approval completes",
    "cooldown": "invitation not sent: this account was emailed moments ago",
    "send_failed": "invitation not sent: the email could not be sent",
    "no_email": "invitation not sent: the account has no email address",
}

#: The invitation outcomes a **named** retry of step 1 re-runs although the step
#: `succeeded`. Only a failed send: the operator is repairing an email that did not
#: go out, which is a decision somebody took. `cooldown` is not here, because the
#: person already has a recent email and a retry after the cooldown would be a
#: second one nobody decided on; `no_email` is not here, because no retry can ever
#: send to an account without an address.
RESENDABLE_INVITATIONS: frozenset[str] = frozenset({"send_failed"})

#: Step 1's code while the invitation has not been asked for. The upsert never
#: asks (celine-eu/onboarding#8), so the service answers this, and it stays on the
#: row until :func:`_send_invitation` replaces it — which happens only once every
#: fail-closed step has succeeded.
AWAITING_INVITATION = "not_requested"


def login_detail(created: bool, invitation: str | None) -> str:
    """Step 1's `detail`: whether the account is new, and whether it can sign in.

    Every outcome is a success. An invitation that did not go out is not a
    failed login: the account exists, and failing closed would block an approval
    over an email. A `send_failed` is repaired by retrying step 1 by name (see
    :data:`RESENDABLE_INVITATIONS`); nothing but that and approval sends one.

    An unknown code is kept visible rather than dropped, so a code the service
    adds later still reaches the operator.
    """
    account = "created" if created else "already existed"
    if invitation is None:
        return account
    return f"{account}, {INVITATION_DETAIL.get(invitation, f'invitation: {invitation}')}"


def _invitation_is_due(ctx: RunContext) -> bool:
    """Whether this run should send step 1's invitation now.

    Only once approval can no longer fail: every fail-closed step `succeeded` or
    `skipped`. Before that, the applicant may never be approved, and an invitation
    to set a password was the defect (celine-eu/onboarding#8). And only while step 1
    still waits for it, so approving again or "retry all" never re-sends.
    """
    login = ctx.rows.get(EnablementStep.KEYCLOAK_USER)
    if login is None:
        return False
    if login.status != EnablementStatus.SUCCEEDED or login.invitation != AWAITING_INVITATION:
        return False
    return _approval_complete(ctx)


def _approval_complete(ctx: RunContext) -> bool:
    """Whether approval can no longer fail: every fail-closed step `succeeded` or `skipped`."""
    return all(
        ctx.rows[spec.step].status in (EnablementStatus.SUCCEEDED, EnablementStatus.SKIPPED)
        for spec in PIPELINE
        if spec.fail_closed
    )


async def _send_invitation(db: AsyncSession, ctx: RunContext) -> None:
    """Send the invitation step 1 held back, and record the outcome on its row.

    Never fails anything: the fail-closed steps have succeeded, so the person is
    approved, and an email is no reason to undo that. What cannot be read as an
    outcome — the service unreachable, a refusal that is not an invitation code,
    this deployment's credential refused — is recorded as `send_failed`, which a
    named retry of step 1 repairs, and the reason goes to the log.
    """
    from celine.onboarding.services import provisioning

    row = ctx.rows[EnablementStep.KEYCLOAK_USER]
    try:
        code = await provisioning.invite_participant(ctx.submission)
    except ConfigurationError as exc:
        logger.error("Could not send the invitation for %s: %s", ctx.submission.ref, exc)
        code = "send_failed"
    except Exception as exc:
        logger.warning("Could not send the invitation for %s: %s", ctx.submission.ref, exc)
        code = "send_failed"

    # The account half of the sentence step 1 wrote, kept: whether this run or an
    # earlier one created the account is on the row and nowhere else.
    row.detail = login_detail((row.detail or "").startswith("created"), code)
    row.invitation = code
    await db.commit()


async def _run_keycloak_user(ctx: RunContext) -> StepOutcome:
    from celine.onboarding.services import provisioning

    if not provisioning.provisioning_enabled():
        return StepOutcome(EnablementStatus.SKIPPED, detail="no provisioning service is configured")
    if await provisioning.participant_community(ctx.submission.rec_slug) is None:
        # No community alias to key the account on, so there is nothing to
        # provision *into* — and a login provisioned here could never be revoked
        # through this seam, because revocation resolves the member through the
        # registry export. Stated as its own reason rather than folded into the
        # one above: an operator seeing this needs to know it is the REC's
        # manifest and not the deployment.
        return StepOutcome(
            EnablementStatus.SKIPPED,
            detail="this community declares no rec_registry binding",
        )

    result = await provisioning.provision_participant(ctx.submission)
    if result is None:
        # Both reasons are checked above, so this is a configuration that
        # changed under a running process.
        return StepOutcome(EnablementStatus.SKIPPED, detail="no login was provisioned")
    # Step 2 registers this as the member's `user_id`, because it is what their
    # token will carry. Read back from Keycloak by the provisioning service
    # rather than assumed: an account that already existed may authenticate
    # under a username that is not their email.
    ctx.keycloak_username = result.username
    return StepOutcome(
        EnablementStatus.SUCCEEDED,
        external_ref=result.user_id,
        detail=login_detail(result.created, result.invitation),
        invitation=result.invitation,
    )


async def _revoke_keycloak_user(ctx: RunContext, row: SubmissionEnablementStep) -> str:
    """Disable the login, addressed by `(community, ref)` rather than by uuid.

    The recorded `external_ref` is the Keycloak uuid, and it is deliberately not
    what this sends: nothing here administers the realm any more, and the
    provisioning service's revocation route takes the community and the member
    key. The uuid stays on the row because the dataspace step reads it.
    """
    from celine.onboarding.services.provisioning import disable_participant

    return await disable_participant(ctx.submission)


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
    from celine.onboarding.services.dataspace_identity import provision_user_identity
    from celine.onboarding.services.provisioning import keycloak_realm

    await provision_user_identity(
        ctx.submission,
        keycloak_user_id=ctx.keycloak_user_id,
        # The realm the user was provisioned in, resolved the same way step 1
        # resolved it — the identity registry is told where to find them, so a
        # second answer to "which realm" is a way for the two to disagree.
        keycloak_realm=(keycloak_realm() if ctx.keycloak_user_id else None),
        # The same value step 2 wrote into `Member.user_id`. The identity
        # registry stores it beside the DID, and the connector reads it back to
        # name this person to the data plane — so the two systems that have to
        # agree are given one value from one source.
        keycloak_username=ctx.keycloak_username,
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
        ctx.submission.rec_slug,
        member_key=member_key,
        did=ctx.submission.dataspace_did,
    )


async def _revoke_dataspace_identity(ctx: RunContext, row: SubmissionEnablementStep) -> str:
    """The membership and the recorded credential, then **every other** credential
    this community linked to the DID, whatever its role.

    ds moves a login to the next community's DID only once the old DID holds no
    active credential (ds ADR-0028), and the member's sharing page may have
    issued one since approval. The same act as the dashboard's release
    (`member_release`), which says how: found through the login, by email here.
    The recorded subject id is cleared, so a re-approval mints a new DID.
    """
    from celine.onboarding.services import member_release

    result = await member_release._identity(ctx.submission, username=None)
    if result.status == member_release.FAILED:
        raise ValueError(result.detail)
    return result.detail


async def _run_dataspace_share(ctx: RunContext) -> StepOutcome:
    from celine.onboarding.config.settings import settings
    from celine.onboarding.services.dataspace_identity import provision_user_shares

    if not settings.ds_connector_url:
        return StepOutcome(EnablementStatus.SKIPPED, detail="no dataspace connector is configured")
    if not ctx.submission.data_sharing_consent and ctx.named != EnablementStep.DATASPACE_SHARE:
        # Nothing to write at approval. Named in a retry it runs all the same: a
        # member who declined on the form may have granted on their page since,
        # and a split they made there is the retry's to converge (the
        # maintainer, 2026-09-19).
        return StepOutcome(EnablementStatus.SKIPPED, detail="no data-sharing consent was given")

    # Every run reads each connector and writes only where it disagrees with the
    # member's newest decision, so a re-run where nothing is split writes nothing
    # and says so.
    written: list[str] = []
    await provision_user_shares(ctx.submission, raise_on_error=True, report=written)
    return StepOutcome(
        EnablementStatus.SUCCEEDED,
        detail=(
            "; ".join(written)
            if written
            else "every connector holding the member's offers records their decision"
        )[:4000],
    )


async def _revoke_dataspace_share(ctx: RunContext, row: SubmissionEnablementStep) -> str:
    from celine.onboarding.config.settings import settings
    from celine.onboarding.services.dataspace_identity import withdraw_user_shares

    if not settings.ds_connector_url:
        return "no dataspace connector is configured"
    if not ctx.submission.dataspace_did:
        return "no dataspace identity is recorded, so there is nobody to withdraw for"

    # Every grant the community collected for the member — the form's and any
    # made on their sharing page since — read from every connector (the
    # maintainer, 2026-09-19). Raises on a refusal or an unreadable connector:
    # returning a string would record the step as undone, which used to file a
    # consent outliving the membership as success.
    written: list[str] = []
    await withdraw_user_shares(ctx.submission, raise_on_error=True, report=written)
    return ("; ".join(written) or "no grant this community collected stood at any connector")[:4000]


def _holds_a_dataspace_identity(submission: Submission) -> bool:
    return bool(submission.dataspace_did)


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
    # When revocation undoes this step, if not only when it `succeeded`. For a
    # step whose effects are not only its own run's — the member's sharing page
    # grants too, and a failed run may have granted at one connector of two — so
    # its row cannot say whether anything stands.
    revoke_when: Callable[[Submission], bool] | None = None
    # A step whose revocation must not run while this one's has failed in the
    # same revocation: it would remove what the other needs to be retried.
    waits_for: EnablementStep | None = None


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
        # Releasing the login resolves the member through the registry; a member
        # deactivated while that failed would be released by nobody here.
        waits_for=EnablementStep.KEYCLOAK_USER,
    ),
    StepSpec(
        EnablementStep.DATASPACE_IDENTITY,
        "Dataspace identity",
        fail_closed=True,
        run=_run_dataspace_identity,
        revoke=_revoke_dataspace_identity,
        # The withdrawal is keyed on the DID this clears, and ds admits the
        # community's read and write only for a member of its organisation —
        # the membership this deletes. Revoked after a failed withdrawal, it
        # left the next revocation nothing to withdraw with, and the grant
        # outlived the membership with no way left to reach it.
        waits_for=EnablementStep.DATASPACE_SHARE,
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
        # Whenever the member holds a dataspace identity, whatever this row
        # says. One offer is recorded at every connector holding its data
        # (ADR-0007), so a `failed` run can have granted at one connector before
        # another refused; and the member's sharing page grants too, so a step
        # `skipped` for a declined form, or `pending`, can stand behind grants
        # (the maintainer, 2026-09-19: every grant is withdrawn).
        revoke_when=_holds_a_dataspace_identity,
    ),
)

SPECS: dict[str, StepSpec] = {spec.step: spec for spec in PIPELINE}


#: The order a revocation runs in, which is **not** `reversed(PIPELINE)`.
#:
#: It was, and the reversal was right while this service administered Keycloak
#: directly: it held the account's uuid on the step row and could disable it
#: whatever the registry said. It does not any more. Revocation now goes through
#: the provisioning service, which resolves `(community, key)` through the
#: registry export — and that export carries only `active` members. So
#: deactivating the member first makes the very next call a `404`: the login
#: survives, the participant can still sign in, and the step row says the
#: revocation succeeded because nothing failed.
#:
#: The login is therefore closed first and the member deactivated after. The
#: rest of the order is unchanged and still reversed: the consent share, then
#: the dataspace identity, then the member.
#:
#: Written out rather than derived, because this is the kind of ordering that a
#: later reader tidies back into `reversed(PIPELINE)` — the list has to be able
#: to say why it is not that.
REVOKE_ORDER: tuple[EnablementStep, ...] = (
    EnablementStep.DATASPACE_SHARE,
    EnablementStep.DATASPACE_IDENTITY,
    EnablementStep.KEYCLOAK_USER,
    EnablementStep.REC_REGISTRY_MEMBER,
)

# Every step is revoked, and exactly once. A step missing from the order above
# would never be undone, and nothing else would say so.
assert set(REVOKE_ORDER) == {spec.step for spec in PIPELINE}
assert len(REVOKE_ORDER) == len(PIPELINE)


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


def revoked(rows: dict[str, SubmissionEnablementStep]) -> bool:
    """Whether enablement was reversed and not run again since.

    `revoke` leaves each step it undid `pending` with `completed_at` set; a run
    never does (it moves a step to `running`, then to an outcome), and a step
    that never ran has no `completed_at`. A re-approval or a retry moves the row
    on, so this answers for the latest state, not for history.
    """
    return any(
        row.status == EnablementStatus.PENDING and row.completed_at is not None
        for row in rows.values()
    )


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
    except ConfigurationError as exc:
        # Not the operator's to fix, and not theirs to read: the message names
        # this deployment's own settings, and it would be shown to every REC
        # operator who pressed Approve. They are told who can act instead, and
        # the detail goes where that person is looking.
        row.status = EnablementStatus.FAILED
        row.last_error = (
            f"{spec.label} is not configured in this deployment. A platform "
            f"operator has to fix it — the details are in the server log."
        )
        row.completed_at = datetime.now(UTC)
        logger.error(
            "Enablement step %s is misconfigured, so %s cannot be enabled: %s",
            spec.step,
            ctx.submission.ref,
            exc,
        )
        await db.commit()
        return row
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
    row.invitation = outcome.invitation
    row.last_error = None
    row.completed_at = datetime.now(UTC)
    await db.commit()
    return row


def _rerun_by_name(spec: StepSpec, row: SubmissionEnablementStep) -> bool:
    """Whether a step named in a retry is re-run although it `succeeded`.

    Two, and nothing else:

    - step 1 whose invitation failed to send, so the operator can send it again;
    - step 4, the sharing consent, **always** (the maintainer, 2026-09-19). A
      member's withdrawal that reaches one connector of two leaves the step
      `succeeded` — nothing about approval failed — and the offer split. Its run
      reads every connector and brings each to the member's newest decision,
      writing nothing where they already agree, so re-running it is the
      re-examination the operator asked for and never a second grant. A step 4
      `skipped` because the member declined on the form is re-run too: they may
      have decided on their page since.
    """
    if spec.step == EnablementStep.DATASPACE_SHARE:
        # `skipped` too: a member who declined everything on the form has step 4
        # skipped, and can still make a split on their page afterwards.
        return row.status in (EnablementStatus.SUCCEEDED, EnablementStatus.SKIPPED)
    if row.status != EnablementStatus.SUCCEEDED:
        return False
    return spec.step == EnablementStep.KEYCLOAK_USER and row.invitation in RESENDABLE_INVITATIONS


async def enable(
    db: AsyncSession, submission: Submission, *, only: str | None = None
) -> dict[str, SubmissionEnablementStep]:
    """Run the pipeline, or one step of it.

    Raises `EnablementError` when a fail-closed step does not succeed, having
    first committed everything that did. A step already `succeeded` or `skipped`
    is not re-run: retry means "finish what is unfinished", not "do it all again".

    **Two exceptions**, and only when the step is named (:func:`_rerun_by_name`):
    step 1 whose invitation came back `send_failed` is re-run, so the operator
    can resend an email that did not go out; and a succeeded step 4 is re-run, so
    the operator can re-drive an offer split across connectors. An unnamed run
    still skips both, so neither approval nor "retry all" re-sends by accident.
    Re-running step 1 is safe because the upsert is idempotent on
    `(community, key)` and the provisioning service applies its send rule again;
    step 4 because it writes only where a connector disagrees with the member's
    newest decision.

    **The invitation is sent after the steps, not by step 1** (celine-eu/onboarding#8):
    only when no fail-closed step is left unfinished, so an approval that fails
    has emailed nobody. See :func:`_invitation_is_due`.
    """
    rows = await ensure_rows(db, submission)
    ctx = RunContext(submission=submission, rows=rows, named=only)

    for spec in PIPELINE:
        if only is not None and spec.step != only:
            continue

        row = rows[spec.step]
        if row.status in (EnablementStatus.SUCCEEDED, EnablementStatus.SKIPPED) and not (
            only is not None and _rerun_by_name(spec, row)
        ):
            continue

        row = await _run_one(db, ctx, spec)
        if row.status == EnablementStatus.FAILED and spec.fail_closed:
            raise EnablementError(
                EnablementStep(spec.step),
                f"{spec.label} could not be provisioned: {row.last_error}",
            )

    # Last, after every step: a retry naming another step is about that step, and
    # sends nobody an email.
    if only in (None, EnablementStep.KEYCLOAK_USER) and _invitation_is_due(ctx):
        await _send_invitation(db, ctx)

    # The account is active: the uploaded bill and identity document have served
    # their purpose, and the check survives in the credential. A no-op on every
    # later run, so a retry or a resend costs one query.
    if _approval_complete(ctx):
        from celine.onboarding.services import document_service

        await document_service.discard_documents(db, submission)

    return rows


async def retry(
    db: AsyncSession, submission: Submission, *, step: str | None = None
) -> dict[str, SubmissionEnablementStep]:
    """Re-run the failed steps, or one named step.

    Naming `keycloak_user` also re-runs a step 1 that succeeded with a
    `send_failed` invitation, and naming `dataspace_share` re-examines a step 4
    that succeeded; see :func:`enable`.

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


#: How a row's ``last_error`` begins when its *revocation* failed, as opposed to
#: its run. Such a row is revoked again by the next revocation.
REVOKE_FAILED = "revoke failed"


def _revoke_failed(row: SubmissionEnablementStep) -> bool:
    return row.status == EnablementStatus.FAILED and (row.last_error or "").startswith(
        REVOKE_FAILED
    )


async def revoke(db: AsyncSession, submission: Submission) -> dict[str, SubmissionEnablementStep]:
    """Undo enablement, in reverse order.

    Best-effort per step and recorded per step: a revocation that fails half way
    must leave a record of what is still out there, because that record is the
    only way anybody finds the rest.
    """
    rows = await ensure_rows(db, submission)
    ctx = RunContext(submission=submission, rows=rows)
    refused: set[EnablementStep] = set()

    for step in REVOKE_ORDER:
        spec = SPECS[step]
        row = rows[spec.step]
        if spec.revoke_when is not None:
            if not spec.revoke_when(submission):
                continue
        elif row.status != EnablementStatus.SUCCEEDED and not _revoke_failed(row):
            # A row whose revocation failed is revoked again: that is what
            # "calling this again is the retry" has to mean for it.
            continue
        if spec.revoke is None:
            logger.info("Step %s has no revocation path; leaving it in place", spec.step)
            continue
        if spec.waits_for in refused:
            # Left `succeeded`, so revoking again undoes it once the step it
            # waits for has been undone. The waited-for row says why.
            waited = rows[spec.waits_for]
            waited.last_error = (
                f"{waited.last_error} — the {spec.label.lower()} is kept until this "
                "succeeds, because it is what the withdrawal needs; revoke again"
            )[:4000]
            logger.warning(
                "Not revoking %s for %s: %s failed first", spec.step, submission.ref, spec.waits_for
            )
            await db.commit()
            continue

        try:
            detail = await spec.revoke(ctx, row)
        except Exception as exc:
            row.status = EnablementStatus.FAILED
            row.last_error = f"{REVOKE_FAILED} — {type(exc).__name__}: {exc}"[:4000]
            logger.warning("Revoking %s failed for %s: %s", spec.step, submission.ref, exc)
            refused.add(spec.step)
            await db.commit()
            continue

        row.status = EnablementStatus.PENDING
        row.detail = detail
        # The invitation belonged to the access just revoked; a re-approval
        # records its own.
        row.invitation = None
        row.last_error = None
        row.external_ref = None
        row.completed_at = datetime.now(UTC)
        await db.commit()

    return rows
