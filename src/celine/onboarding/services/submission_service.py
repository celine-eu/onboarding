from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from celine.onboarding.models.submission import Submission, SubmissionStatus
from celine.onboarding.models.schemas import ConsentCreate, SubmissionUpdate
from celine.onboarding.workflows.engine import validate_transition, can_submit, InvalidTransition


def _assert_phone_verified(submission: Submission) -> None:
    """Block approval when the REC requires phone verification and it is missing.

    Only enforced for RECs whose manifest lists the `phone_verify` step, so RECs
    that never opted into SMS verification are unaffected.
    """
    from celine.onboarding.services import template_service

    manifest = template_service.load_manifest(submission.rec_slug)
    if "phone_verify" in manifest.get("steps", []) and not submission.phone_verified:
        raise ValueError("Cannot approve: phone number is not verified")


async def create_from_consent(
    db: AsyncSession, data: ConsentCreate, client_ip: str, rec_slug: str,
) -> Submission:
    now = datetime.now(timezone.utc)

    submission = Submission(
        rec_slug=rec_slug,
        consent_ip=client_ip,
        gdpr_consent=data.gdpr_consent,
        gdpr_consent_at=now if data.gdpr_consent else None,
        gdpr_consent_version=data.gdpr_consent_version,
        policy_consent=data.policy_consent,
        policy_consent_at=now if data.policy_consent else None,
        policy_consent_version=data.policy_consent_version,
        statute_consent=data.statute_consent,
        statute_consent_at=now if data.statute_consent else None,
        statute_consent_version=data.statute_consent_version,
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)
    return submission


async def get_submission(db: AsyncSession, submission_id: uuid.UUID) -> Submission | None:
    result = await db.execute(
        select(Submission)
        .where(Submission.id == submission_id)
        .options(selectinload(Submission.documents))
    )
    return result.scalar_one_or_none()


async def list_submissions(
    db: AsyncSession, *, rec_slug: str, skip: int = 0, limit: int = 50,
) -> list[Submission]:
    result = await db.execute(
        select(Submission)
        .where(Submission.rec_slug == rec_slug)
        .order_by(Submission.created_at.desc())
        .offset(skip).limit(limit)
    )
    return list(result.scalars().all())


async def update_submission(
    db: AsyncSession,
    submission: Submission,
    data: SubmissionUpdate,
    background_tasks: BackgroundTasks | None = None,
) -> Submission:
    updates = data.model_dump(exclude_unset=True)
    now = datetime.now(timezone.utc)

    if "statute_consent" in updates and updates["statute_consent"] and not submission.statute_consent:
        updates["statute_consent_at"] = now

    if (
        "data_sharing_consent" in updates
        and updates["data_sharing_consent"]
        and not submission.data_sharing_consent
    ):
        updates["data_sharing_consent_at"] = now

    target_status = updates.pop("status", None)

    for key, value in updates.items():
        setattr(submission, key, value)

    if target_status is not None:
        validate_transition(submission.status, target_status)
        if target_status == SubmissionStatus.SUBMITTED:
            errors = can_submit(submission)
            if errors:
                raise ValueError(f"Cannot submit: {'; '.join(errors)}")
        if target_status == SubmissionStatus.APPROVED:
            _assert_phone_verified(submission)
        submission.status = target_status
        if target_status == SubmissionStatus.APPROVED:
            from celine.onboarding.config.settings import settings
            from celine.onboarding.services.dataspace_identity import provision_user_identity
            from celine.onboarding.services.keycloak_identity import provision_keycloak_user
            from celine.onboarding.services.rec_registry import register_member

            # Approval enables somebody, which means three things in this order:
            # a login, a community member, then a dataspace identity.
            #
            # The order is load-bearing rather than stylistic. The registry keys
            # a member on (community, user_id), so the Keycloak user exists
            # first; the dataspace identity is last because it is the step that
            # can be retried afterwards.
            kc_result = await provision_keycloak_user(submission)

            # Fails closed, unlike share provisioning. A participant missing
            # from the registry is enabled in name only — invisible to every
            # pipeline and dashboard, which all join on it — and that is not a
            # state anything downstream can work around.
            await register_member(
                submission,
                keycloak_user_id=kc_result.user_id if kc_result else None,
            )

            await provision_user_identity(
                submission,
                keycloak_user_id=kc_result.user_id if kc_result else None,
                keycloak_realm=(
                    settings.dataspace_keycloak_realm if kc_result else None
                ),
            )

    await db.commit()

    reloaded = await get_submission(db, submission.id)

    if target_status == SubmissionStatus.SUBMITTED and reloaded:
        if background_tasks is not None:
            from celine.onboarding.services.notification_service import (
                handle_submission_notification,
            )

            background_tasks.add_task(handle_submission_notification, reloaded)

    return reloaded
