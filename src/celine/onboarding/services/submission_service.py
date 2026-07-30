from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from celine.onboarding.models.submission import Submission
from celine.onboarding.models.schemas import ConsentCreate, SubmissionUpdate
from celine.onboarding.services.audit_service import Actor
from celine.onboarding.workflows.engine import InvalidTransition


def _assert_phone_verified(submission: Submission) -> None:
    """Kept as an alias — the implementation lives in `services/review`."""
    from celine.onboarding.services.review import _assert_phone_verified as impl

    impl(submission)


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
        # One implementation of the state machine, shared with the admin API and
        # the CLI — including the enablement pipeline that runs on approval. This
        # used to inline all of it, which is how the CLI and the console could
        # have drifted into reaching states each other refused.
        from celine.onboarding.services import review

        await review.transition(
            db,
            submission,
            target_status,
            actor=Actor.system("wizard"),
            background_tasks=background_tasks,
        )
    else:
        await db.commit()

    return await get_submission(db, submission.id)
