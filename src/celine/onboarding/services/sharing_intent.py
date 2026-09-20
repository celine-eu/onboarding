"""The member's own record of their newest decision per offer (their *intent*).

The maintainer decided on 2026-09-19 that the member's toggle records what they
decided before it tells any connector, and that the operator's retry ranks that
record with what the connectors say. Two things no connector can be relied on to
keep are why:

* **A withdrawal over a withdrawal is not stamped.** ds changes nothing but whose
  refusal it is, so a member who withdraws while one connector already shows
  ``revoked`` and the only one still granting refuses leaves no time anywhere —
  and the retry, ranking what it can read, re-drives the older grant.
* **A relayed grant replaces the row a withdrawal left.** A retry that read a
  connector just before the member withdrew there writes its grant over that
  withdrawal; if the member's withdrawal failed everywhere else, no connector
  shows it any more.

Only :mod:`member_sharing` writes here, and only :func:`provision_user_shares
<celine.onboarding.services.dataspace_identity.provision_user_shares>` reads.
Both call through the module (``sharing_intent.record``), never a from-import, so
the unit tests can stand an in-memory store in for the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from celine.onboarding.models.sharing_intent import MemberSharingIntent


@dataclass(frozen=True, slots=True)
class Intent:
    """One member's newest decision on one offer, as they took it."""

    offer_id: str
    granted: bool
    decided_at: datetime
    evidence: dict[str, Any] | None = None


def upsert_statement(
    *,
    subject_id: str,
    rec_slug: str,
    offer_id: str,
    granted: bool,
    decided_at: datetime,
    evidence: dict[str, Any] | None,
):
    """The one write: insert, or replace the member's earlier decision on the offer.

    Idempotent per ``(subject_id, offer_id)`` — one row, whatever the number of
    presses. A press repeating the standing decision still moves ``decided_at``:
    it is a new act, and a repeated withdrawal being dated is exactly what the
    connectors do not do.
    """
    stmt = insert(MemberSharingIntent).values(
        subject_id=subject_id,
        rec_slug=rec_slug,
        offer_id=offer_id,
        granted=granted,
        decided_at=decided_at,
        evidence=evidence,
    )
    return stmt.on_conflict_do_update(
        constraint="uq_member_sharing_intent_subject_offer",
        set_={
            "rec_slug": stmt.excluded.rec_slug,
            "granted": stmt.excluded.granted,
            "decided_at": stmt.excluded.decided_at,
            "evidence": stmt.excluded.evidence,
            "updated_at": datetime.now(UTC),
        },
    )


async def record(
    *,
    subject_id: str,
    rec_slug: str,
    offer_id: str,
    granted: bool,
    evidence: dict[str, Any] | None,
) -> Intent:
    """Record the member's decision, **committed**, and return it.

    Committed in a session of its own before the caller relays anything, so a
    relay that fails afterwards — at one connector or at every one — cannot take
    the record with it. Raises when the database refuses; the caller then relays
    nothing.
    """
    from celine.onboarding.models.database import async_session

    decided_at = datetime.now(UTC)
    async with async_session() as db:
        await db.execute(
            upsert_statement(
                subject_id=subject_id,
                rec_slug=rec_slug,
                offer_id=offer_id,
                granted=granted,
                decided_at=decided_at,
                evidence=evidence,
            )
        )
        await db.commit()
    return Intent(offer_id=offer_id, granted=granted, decided_at=decided_at, evidence=evidence)


async def for_subject(subject_id: str) -> dict[str, Intent]:
    """Every offer this member decided on their page, newest decision each.

    Raises when the database cannot be read: the retry then does not know the
    member's newest decision, and writes nothing.
    """
    from celine.onboarding.models.database import async_session

    async with async_session() as db:
        rows = (
            (
                await db.execute(
                    select(MemberSharingIntent).where(MemberSharingIntent.subject_id == subject_id)
                )
            )
            .scalars()
            .all()
        )
    return {
        row.offer_id: Intent(
            offer_id=row.offer_id,
            granted=row.granted,
            decided_at=row.decided_at,
            evidence=row.evidence,
        )
        for row in rows
    }
