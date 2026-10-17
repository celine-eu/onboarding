"""What a member accepted when they applied, read back by them — `/api/me/acceptances`.

The web app's terms gate asks this on a miss: a document whose version **and** sha256
match what the legal host serves now was accepted here, and is not asked again. Read-only,
from the consent columns the submission already holds.

**Which application is the member's** is the one question this module answers, and it is
answered by evidence, never by a guess:

- the token's `sub` is the Keycloak uuid, and the login step of enablement
  (`keycloak_user`) recorded the uuid of the account it provisioned or adopted as its
  `external_ref`. Neither the email nor the username is consulted: an adopted account's
  username is not its email (`two-keycloak-username-conventions`), and an email names
  whoever typed it into a form;
- the step must have **succeeded** and not been revoked. A revocation clears the
  `external_ref`, so a released member's old application no longer matches;
- the application must be **approved**, and in the community the token's organization
  resolves to (`member_sharing.resolve_member_rec`). A person released from one community
  and joined to another keeps one Keycloak account, so the uuid alone could name an
  application in the community they left.

The REC is never taken from the caller, and the route takes no parameter at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from celine.sdk.auth import JwtUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.models.enablement import (
    EnablementStatus,
    EnablementStep,
    SubmissionEnablementStep,
)
from celine.onboarding.models.submission import Submission, SubmissionStatus
from celine.onboarding.services import legal_documents, member_sharing, template_service


class NotAMemberTokenError(PermissionError):
    """A token with no person behind it: a service account has no application."""


class AmbiguousCommunityError(ValueError):
    """The token's organizations resolve to more than one community served here."""


@dataclass(frozen=True)
class Acceptance:
    slot: str
    legal_slot: str
    version: str
    url: str | None
    sha256: str | None
    accepted_at: datetime | None


@dataclass(frozen=True)
class MemberAcceptances:
    submission_id: str | None = None
    rec_slug: str | None = None
    acceptances: list[Acceptance] = field(default_factory=list)


def application_query(*, rec_slug: str, keycloak_user_id: str):
    """The member's approved application in *rec_slug*, found through their login step."""
    return (
        select(Submission)
        .join(SubmissionEnablementStep, SubmissionEnablementStep.submission_id == Submission.id)
        .where(
            SubmissionEnablementStep.step == EnablementStep.KEYCLOAK_USER.value,
            SubmissionEnablementStep.status == EnablementStatus.SUCCEEDED.value,
            SubmissionEnablementStep.external_ref == keycloak_user_id,
            Submission.rec_slug == rec_slug,
            Submission.status == SubmissionStatus.APPROVED,
        )
        .order_by(Submission.created_at.desc())
        .limit(1)
    )


def acceptances_of(submission: Submission) -> list[Acceptance]:
    """The documents accepted on *submission*, each with a version to compare against.

    A slot accepted without a known version (the legal host had not answered) is left out:
    there is nothing for the web app to match, so it asks.
    """
    legal_slot = {
        **legal_documents.LEGAL_SLOT,
        legal_documents.CLAUSES_SLOT: legal_documents.CLAUSES_SLOT,
    }
    found: list[Acceptance] = []
    for slot in template_service.CONSENT_SLOTS:
        version = getattr(submission, f"{slot}_consent_version", None)
        if not getattr(submission, f"{slot}_consent", False) or not version:
            continue
        found.append(
            Acceptance(
                slot=slot,
                legal_slot=legal_slot[slot],
                version=str(version),
                url=getattr(submission, f"{slot}_consent_url", None),
                sha256=getattr(submission, f"{slot}_consent_sha256", None),
                accepted_at=getattr(submission, f"{slot}_consent_at", None),
            )
        )
    return found


async def get_acceptances(db: AsyncSession, user: JwtUser) -> MemberAcceptances:
    """The acceptances of the member's own application; empty when there is none."""
    if user.is_service_account:
        raise NotAMemberTokenError("a service account has no application")
    if not user.sub:
        raise NotAMemberTokenError("the token names no subject")

    await template_service.ensure_fresh()
    try:
        rec_slug = member_sharing.resolve_member_rec(user)
    except ValueError as exc:
        raise AmbiguousCommunityError(str(exc)) from exc
    if rec_slug is None:
        return MemberAcceptances()

    result = await db.execute(application_query(rec_slug=rec_slug, keycloak_user_id=user.sub))
    submission = result.scalars().first()
    if submission is None:
        return MemberAcceptances(rec_slug=rec_slug)
    return MemberAcceptances(
        submission_id=str(submission.id),
        rec_slug=rec_slug,
        acceptances=acceptances_of(submission),
    )
