"""Moving a submission through the review state machine.

One implementation, three callers: the public wizard (draft → submitted), the
admin API, and `onboarding-cli` in `--local` mode. Keeping them on the same
function is what stops the CLI from becoming a way to reach a state the API
refuses.
"""

from __future__ import annotations

import logging

from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.models.submission import Submission, SubmissionStatus
from celine.onboarding.services import audit_service, enablement
from celine.onboarding.services.audit_service import Actor
from celine.onboarding.workflows.engine import can_submit, validate_transition

logger = logging.getLogger(__name__)


def _assert_phone_verified(submission: Submission) -> None:
    """Block approval when the REC requires phone verification and it is missing.

    Only enforced for RECs whose manifest lists the `phone_verify` step, so RECs
    that never opted into SMS verification are unaffected.
    """
    from celine.onboarding.services import template_service

    manifest = template_service.load_manifest(submission.rec_slug)
    if "phone_verify" in manifest.get("steps", []) and not submission.phone_verified:
        raise ValueError("Cannot approve: phone number is not verified")


def check(submission: Submission, target: SubmissionStatus) -> None:
    """Everything that must hold before a transition, with nothing written yet.

    Separated so a caller can ask "would this be allowed?" — the console greys out
    an Approve button rather than offering one that 422s.
    """
    validate_transition(submission.status, target)

    if target == SubmissionStatus.SUBMITTED:
        errors = can_submit(submission)
        if errors:
            raise ValueError(f"Cannot submit: {'; '.join(errors)}")

    if target == SubmissionStatus.APPROVED:
        _assert_phone_verified(submission)


async def transition(
    db: AsyncSession,
    submission: Submission,
    target: SubmissionStatus,
    *,
    actor: Actor,
    reason: str | None = None,
    ip: str | None = None,
    rec_slug: str | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> Submission:
    """Move *submission* to *target*, enabling the person if that is approval.

    On approval the enablement pipeline runs **before** the status changes, so a
    fail-closed step leaves the submission in review rather than approved-but-not-
    enabled. What the pipeline did manage to do is still committed — see
    `services/enablement`.
    """
    previous = submission.status
    check(submission, target)

    if target == SubmissionStatus.APPROVED:
        # The status is deliberately not set yet: the person is not enabled, so
        # they are not approved.
        try:
            await enablement.enable(db, submission)
        except enablement.EnablementError as exc:
            # The attempt is worth recording even though it changed no status.
            # The step rows say what broke; only the audit trail says who tried,
            # and "nobody ever tried to approve this" is a different fact from
            # "somebody tried and the registry was down".
            await audit_service.record_and_commit(
                db,
                action="transition_failed",
                entity_type="submission",
                entity_id=str(submission.id),
                actor=actor,
                rec_slug=rec_slug or submission.rec_slug,
                ip=ip,
                detail=f"{previous.value} -> {target.value} blocked at {exc.step}: {exc.message}",
            )
            raise

    submission.status = target

    detail = f"{previous.value} -> {target.value}"
    if reason:
        detail = f"{detail} — {reason}"
    audit_service.record(
        db,
        action="transition",
        entity_type="submission",
        entity_id=str(submission.id),
        actor=actor,
        rec_slug=rec_slug or submission.rec_slug,
        ip=ip,
        detail=detail,
    )

    # The audit row and the status change commit together. They used to be two
    # commits, so a crash between them left a change nobody was recorded as making.
    await db.commit()
    await db.refresh(submission)

    if target == SubmissionStatus.SUBMITTED and background_tasks is not None:
        from celine.onboarding.services.notification_service import (
            handle_submission_notification,
        )

        background_tasks.add_task(handle_submission_notification, submission)

    return submission
