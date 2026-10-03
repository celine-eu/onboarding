from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import BackgroundTasks
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from celine.onboarding.models.schemas import ConsentCreate, SubmissionUpdate
from celine.onboarding.models.submission import Submission, SubmissionStatus
from celine.onboarding.services import existing_member
from celine.onboarding.services.audit_service import Actor


def _assert_phone_verified(submission: Submission) -> None:
    """Kept as an alias — the implementation lives in `services/review`."""
    from celine.onboarding.services.review import _assert_phone_verified as impl

    impl(submission)


class ConsentNotGivenError(ValueError):
    """A document the community asks to accept was not accepted."""


def _consents(data: ConsentCreate, asked: tuple[str, ...], now: datetime) -> dict:
    """The consent columns: the asked slots as given, the others never recorded."""
    from celine.onboarding.services import template_service

    missing = [slot for slot in asked if not getattr(data, f"{slot}_consent")]
    if missing:
        raise ConsentNotGivenError(f"Required consent not given: {', '.join(missing)}")
    columns: dict = {}
    for slot in template_service.CONSENT_SLOTS:
        given = slot in asked and getattr(data, f"{slot}_consent")
        columns[f"{slot}_consent"] = given
        columns[f"{slot}_consent_at"] = now if given else None
        columns[f"{slot}_consent_version"] = getattr(data, f"{slot}_consent_version") if given else None
    return columns


async def create_from_consent(
    db: AsyncSession,
    data: ConsentCreate,
    client_ip: str,
    rec_slug: str,
) -> Submission:
    from celine.onboarding.services import template_service

    now = datetime.now(UTC)
    if data.declared_existing_member:
        existing_member.assert_offered(rec_slug)

    submission = Submission(
        rec_slug=rec_slug,
        consent_ip=client_ip,
        **_consents(data, template_service.consent_slots(rec_slug), now),
        declared_existing_member=data.declared_existing_member,
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


def _queue_filters(
    query,
    *,
    rec_slug: str,
    status: SubmissionStatus | None = None,
    ref: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    declared_existing_member: bool | None = None,
):
    """Shared between the list and its count, so the two cannot disagree.

    Text search matches the **reference only**. Names, emails, fiscal codes and
    PODs are Fernet-encrypted with a non-deterministic IV, so there is no
    ciphertext to compare against — searching them would mean decrypting every row
    in the community on every keystroke. The reference is printed on the
    participant's confirmation and quoted in every email, so it is what an
    operator has to hand anyway.
    """
    query = query.where(Submission.rec_slug == rec_slug)
    if status is not None:
        query = query.where(Submission.status == status)
    if ref:
        query = query.where(Submission.ref.ilike(f"%{ref.strip()}%"))
    if created_from is not None:
        query = query.where(Submission.created_at >= created_from)
    if created_to is not None:
        query = query.where(Submission.created_at <= created_to)
    if declared_existing_member is not None:
        query = query.where(Submission.declared_existing_member == declared_existing_member)
    return query


async def list_submissions(
    db: AsyncSession,
    *,
    rec_slug: str,
    skip: int = 0,
    limit: int = 50,
    status: SubmissionStatus | None = None,
    ref: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    declared_existing_member: bool | None = None,
) -> list[Submission]:
    query = _queue_filters(
        select(Submission),
        rec_slug=rec_slug,
        status=status,
        ref=ref,
        created_from=created_from,
        created_to=created_to,
        declared_existing_member=declared_existing_member,
    )
    result = await db.execute(
        query.order_by(Submission.created_at.desc()).offset(skip).limit(limit)
    )
    return list(result.scalars().all())


async def count_submissions(
    db: AsyncSession,
    *,
    rec_slug: str,
    status: SubmissionStatus | None = None,
    ref: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    declared_existing_member: bool | None = None,
) -> int:
    """The total behind a filtered page, for `X-Total-Count`.

    Without it the console cannot paginate: it has no way to tell a full last page
    from a page that happens to be full.
    """
    query = _queue_filters(
        select(func.count()).select_from(Submission),
        rec_slug=rec_slug,
        status=status,
        ref=ref,
        created_from=created_from,
        created_to=created_to,
        declared_existing_member=declared_existing_member,
    )
    return int((await db.execute(query)).scalar_one())


async def queue_stats(db: AsyncSession, *, rec_slug: str) -> dict[str, int]:
    """How many submissions sit in each status, including the empty ones.

    Statuses with no rows are reported as zero rather than omitted — a console
    that hides "0 awaiting review" makes an empty queue indistinguishable from a
    broken filter.
    """
    result = await db.execute(
        select(Submission.status, func.count())
        .where(Submission.rec_slug == rec_slug)
        .group_by(Submission.status)
    )
    counts = {status.value: 0 for status in SubmissionStatus}
    for status, count in result.all():
        counts[status.value] = int(count)
    return counts


async def enablement_stats(db: AsyncSession, *, rec_slug: str) -> dict[str, int]:
    """How many approved participants have an enablement step still failing.

    The number an operator acts on: a person approved but not actually enabled is
    invisible to every pipeline downstream.
    """
    from celine.onboarding.models.enablement import (
        EnablementStatus,
        SubmissionEnablementStep,
    )

    result = await db.execute(
        select(func.count(func.distinct(SubmissionEnablementStep.submission_id)))
        .select_from(SubmissionEnablementStep)
        .join(Submission, Submission.id == SubmissionEnablementStep.submission_id)
        .where(Submission.rec_slug == rec_slug)
        .where(SubmissionEnablementStep.status == EnablementStatus.FAILED)
    )
    return {"submissions_with_failed_steps": int(result.scalar_one())}


async def _validate_sharing_offer_ids(rec_slug: str, offer_ids: list[str]) -> None:
    """Refuse offer ids this REC does not offer, or does not offer for consent.

    The ids arrive from the client and were previously stored on the word of
    whoever sent them. The connector then refuses a contract-based offer with a
    409 (*"it is disclosed, not consented"*) and an unknown one with a 422 — but
    only at provisioning, days later, by which time the failure reads as
    ``share_provisioned = false`` and the member has quietly dropped out of every
    POD export. Checking here turns that into a 422 the wizard can act on while
    the person is still in front of it.

    **Contract-based offers are rendered and never consented.** A manifest
    allow-list may legitimately name one — the statute step discloses it without a
    toggle — so its presence in ``get_sharing_offers`` is not an error. Recording
    it as something the person *consented* to is, because it is a claim about them
    that is not true. That asymmetry is deliberate and mirrors the connector's:
    ``POST /consent/admin/shares`` refuses a contract offer, ``POST
    /admin/disclosure`` accepts one.

    An accepted offer's ``requires_offers``, as the connector publishes it, must be
    accepted too, for every prerequisite this REC publishes.

    Fails closed when the vocabulary cannot be reached: an unverifiable consent is
    not recorded. `SharingOffersUnavailableError` propagates for the route to answer
    503, which is the honest code — the claim is not wrong, it is unchecked.
    """
    from celine.onboarding.services import template_service

    offers = await template_service.get_sharing_offers(rec_slug)
    by_id = {str(o.get("id")): o for o in offers if o.get("id")}

    unknown = [i for i in offer_ids if i not in by_id]
    if unknown:
        raise ValueError(
            "Unknown data-sharing offer(s) for this community: "
            f"{', '.join(sorted(unknown))}. "
            f"Offered here: {', '.join(sorted(by_id)) or '(none)'}"
        )

    contract_based = [i for i in offer_ids if not by_id[i].get("requires_consent")]
    if contract_based:
        raise ValueError(
            "These offers are disclosed, not consented, and cannot be recorded as "
            f"a data-sharing consent: {', '.join(sorted(contract_based))}"
        )

    # **An offer is gated by its own prerequisites, not by the manifest's
    # `primary`.** A use whose data exists only because of another offer — a
    # community's use of readings a distributor releases to it — declares that
    # offer in `requires_offers`, and accepting it alone would record a consent
    # to a use of data that will never arrive. An offer that declares nothing is
    # its own decision, even when `primary` is set: `primary` orders the step, it
    # does not bundle another party's offer with it. A prerequisite this REC does
    # not publish gates nothing — the person could not have been asked. The
    # wizard enforces the same rule; this is where a client that does not is refused.
    accepted = set(offer_ids)
    unmet = {
        i: sorted(r for r in by_id[i].get("requires_offers") or [] if r in by_id and r not in accepted)
        for i in offer_ids
    }
    unmet = {i: r for i, r in unmet.items() if r}
    if unmet:
        raise ValueError(
            "; ".join(
                f"{i!r} depends on {', '.join(repr(r) for r in reqs)}, which was not accepted"
                for i, reqs in sorted(unmet.items())
            )
        )


async def _validate_presented_offers(
    rec_slug: str, presented: list[dict], accepted: list[str] | None
) -> None:
    """Refuse a presented set that is not what this REC publishes right now.

    The set is what the web app trusts to decide that a member has already been
    asked, so a wrong one silences a question. Each entry must be a consent-based
    offer this REC publishes, at its current version; and an accepted offer that
    was not presented is a claim about the step that cannot be true.
    """
    from celine.onboarding.services import template_service

    offers = await template_service.get_sharing_offers(rec_slug)
    by_id = {str(o.get("id")): o for o in offers if o.get("id")}

    problems: list[str] = []
    for item in presented:
        offer = by_id.get(item["id"])
        if offer is None or not offer.get("requires_consent"):
            problems.append(f"{item['id']} is not a consent-based offer of this community")
        elif item.get("version") != offer.get("consent_text_version"):
            problems.append(
                f"{item['id']} was presented at version {item.get('version')!r}, "
                f"but the published one is {offer.get('consent_text_version')!r}"
            )
    missing = sorted(set(accepted or []) - {item["id"] for item in presented})
    if missing:
        problems.append(f"accepted but not presented: {', '.join(missing)}")
    if problems:
        raise ValueError("Presented data-sharing offers refused: " + "; ".join(problems))


async def update_submission(
    db: AsyncSession,
    submission: Submission,
    data: SubmissionUpdate,
    background_tasks: BackgroundTasks | None = None,
) -> Submission:
    updates = data.model_dump(exclude_unset=True)
    now = datetime.now(UTC)

    if "statute_consent" in updates:
        from celine.onboarding.services import template_service

        if "statute" not in template_service.consent_slots(submission.rec_slug):
            # The community asks for no statute: nothing to record, whatever is sent.
            updates.pop("statute_consent")
    if (
        "statute_consent" in updates
        and updates["statute_consent"]
        and not submission.statute_consent
    ):
        updates["statute_consent_at"] = now

    if (
        "data_sharing_consent" in updates
        and updates["data_sharing_consent"]
        and not submission.data_sharing_consent
    ):
        updates["data_sharing_consent_at"] = now

    # Validated on the ids rather than on the consent flag, so it holds whichever
    # order the two arrive in: the flag with the ids beside it (what the wizard
    # sends, and what `SubmissionUpdate` already requires of that payload), or the
    # ids alone in an earlier PATCH.
    if updates.get("data_sharing_consent_offer_ids"):
        await _validate_sharing_offer_ids(
            submission.rec_slug, list(updates["data_sharing_consent_offer_ids"])
        )

    if updates.get("data_sharing_offers_presented") is not None:
        await _validate_presented_offers(
            submission.rec_slug,
            list(updates["data_sharing_offers_presented"]),
            updates.get("data_sharing_consent_offer_ids"),
        )

    if updates.get("declared_existing_member"):
        existing_member.assert_offered(submission.rec_slug)
    if updates.get("declared_existing_member") is None:
        updates.pop("declared_existing_member", None)

    target_status = updates.pop("status", None)

    from celine.onboarding.services import supply_boundary

    address_before = supply_boundary.supply_address(submission)
    for key, value in updates.items():
        setattr(submission, key, value)

    # The boundary follows the supply address the submission holds, resolved
    # here and never taken from the client (REQ-0007). A submit resolves again
    # in `review.transition`, whichever caller drives it.
    if target_status != SubmissionStatus.SUBMITTED:
        await supply_boundary.refresh_on_save(submission, address_before=address_before)

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
