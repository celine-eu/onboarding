"""The community's assertion that a member holds the supply points it shares (R4).

A holder — a grid operator — releases a member's readings by supply point, and has
agreed to do so **on the community's word**: it does not check who holds a POD. So
every grant that carries a member's ``pod:`` keys to a holder carries the word
itself, ``key_assertion`` (the R4 contract, ds ``AdminShareLegalBasis``):

    {terms, terms_sha256, method, verification_ref, verified_by, verified_at,
     evidence: [{kind, sha256}, ...]}

What it lets an auditor reconstruct, from the holder's records alone: **what** was
asserted (the keys, in the grant), **by whom** (the collector, from its token; the
verifying operator as a pseudonym only this deployment resolves), **on what
evidence** (digests of files the community keeps), **under which responsibility
statement** (the terms version and the sha256 of its exact text), and **when**.

Codes and hashes only: no fiscal code, no POD, no hash of either — both are
low-entropy and a hash of one is reversible by enumeration — and no name or email.

``offline`` alone is never asserted: the requester's decision (2026-10-05) is that
an offline check counts only with a digest of the evidence the community holds
(``offline-with-evidence``). See ``docs/specifications/pod-ownership-assertion.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from celine.sdk.posture import PostureGuard, is_dev

from celine.onboarding.config.settings import settings
from celine.onboarding.models.document import DocumentType
from celine.onboarding.models.verification import VerificationMethod

if TYPE_CHECKING:
    from celine.onboarding.models.submission import Submission
    from celine.onboarding.models.verification import SubmissionVerification

logger = logging.getLogger(__name__)

#: The generic draft the service ships, used when `REC_ASSERTION_TERMS_FILE` is empty.
DRAFT_TERMS_FILE = Path(__file__).resolve().parent.parent / "terms" / "rec-pod-assertion-1.md"

#: The contract's evidence kinds, by the document type a digest came from.
EVIDENCE_KINDS: dict[DocumentType, str] = {
    DocumentType.UTILITY_BILL: "utility_bill",
    DocumentType.ID_CARD: "id_document",
}
EVIDENCE_KIND_VALUES = frozenset({"utility_bill", "id_document", "other"})

#: Only in development, and only when no key is set: `verified_by` still has the
#: shape the connector checks. Refused outside dev (`posture`).
_DEV_HMAC_KEY = b"onboarding-dev-only-rec-assertion-key"

#: What a key looks like in a grant. A grant without one needs no assertion.
POD_KEY_PREFIX = "pod:"


class AssertionUnavailableError(ValueError):
    """No assertion can be built: the reason is written for the community's operator."""


def evidence_kind(doc_type: DocumentType | str | None) -> str:
    try:
        return EVIDENCE_KINDS.get(DocumentType(doc_type), "other")
    except ValueError:
        return "other"


def enabled(env: str | None = None) -> bool:
    """Whether grants at a holder carry the assertion (`DS_KEY_ASSERTION`).

    Unset means on everywhere but `CELINE_ENV=dev`: the local stacks pin a ds
    whose `AdminShareLegalBasis` predates the field and refuses it (422).
    """
    if settings.ds_key_assertion is not None:
        return bool(settings.ds_key_assertion)
    if env is not None:
        return env != "dev"
    return not is_dev()


def grants_at_a_holder(rec_slug: str) -> bool:
    """Whether this community registers grants at another participant's connector.

    Those are the grants that carry the member's supply points.
    """
    from celine.onboarding.services import template_service

    try:
        binding = template_service.dataspace_binding(rec_slug)
    except Exception:  # noqa: BLE001 — an unknown or unbound community holds nothing
        return False
    return bool(settings.dataspace_enabled and binding.enabled and binding.connectors)


def evidence_required(rec_slug: str) -> bool:
    """Whether a verification here must carry an evidence digest (REQ-0042)."""
    return bool(enabled() and grants_at_a_holder(rec_slug))


def carries_pod_keys(keys: list[str] | None) -> bool:
    return bool(keys) and any(str(k).startswith(POD_KEY_PREFIX) for k in keys or [])


@dataclass(frozen=True, slots=True)
class Terms:
    id: str
    sha256: str
    path: Path

    @property
    def is_draft(self) -> bool:
        return self.path == DRAFT_TERMS_FILE


def terms() -> Terms:
    """The responsibility statement in force: its id and the sha256 of its exact bytes.

    Read on every call — it is small — so a replaced file is never asserted under
    the old hash.
    """
    path = (
        settings.resolve_path(settings.rec_assertion_terms_file)
        if settings.rec_assertion_terms_file.strip()
        else DRAFT_TERMS_FILE
    )
    try:
        text = path.read_bytes()
    except OSError as exc:
        raise AssertionUnavailableError(
            "the community's statement on supply point holders cannot be read from "
            "this deployment's configuration — see the server log"
        ) from exc
    return Terms(
        id=settings.rec_assertion_terms_id, sha256=hashlib.sha256(text).hexdigest(), path=path
    )


def _hmac_key() -> bytes:
    key = settings.rec_assertion_hmac_key.strip()
    if key:
        return key.encode()
    if is_dev():
        return _DEV_HMAC_KEY
    # Startup refuses this (`posture`); this is the backstop.
    raise AssertionUnavailableError(
        "REC_ASSERTION_HMAC_KEY is not set, so the verifying operator cannot be named "
        "by pseudonym — see the server log"
    )


def verifier_pseudonym(row: SubmissionVerification) -> str:
    """``verified_by``: HMAC-SHA256 of who recorded the verification.

    The operator's subject when there is one, else the client or the actor type.
    Only this deployment holds the key, so only it can tell who verified.
    """
    who = row.actor_sub or row.actor_client_id or f"{row.actor_type}:unknown"
    return hmac.new(_hmac_key(), who.encode(), hashlib.sha256).hexdigest()


def build(row: SubmissionVerification | None) -> dict[str, Any]:
    """The assertion citing ``row``, or why there can be none.

    Raises :class:`AssertionUnavailableError` with a sentence the community's
    operator can act on.
    """
    if row is None:
        raise AssertionUnavailableError(
            "no verification of the member is recorded, so the community cannot assert "
            "that they hold their supply points — record one with evidence"
        )
    method = VerificationMethod(row.method)
    evidence = [
        {"kind": item["kind"], "sha256": item["sha256"]}
        for item in (row.evidence or [])
        if isinstance(item, dict) and item.get("sha256")
    ]
    if not method.carries_evidence or not evidence:
        raise AssertionUnavailableError(
            "the verification in force records no evidence digest (an offline check "
            "without the evidence file, or a document uploaded before digests were "
            "kept), so the community cannot assert that the member holds their supply "
            "points — record a new verification with the evidence file or an uploaded "
            "document, then retry"
        )
    statement = terms()
    verified_at = row.created_at
    if verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=UTC)
    return {
        "terms": statement.id,
        "terms_sha256": statement.sha256,
        "method": method.value,
        "verification_ref": str(row.id),
        "verified_by": verifier_pseudonym(row),
        "verified_at": verified_at.astimezone(UTC).isoformat(),
        "evidence": evidence,
    }


async def for_subject(rec_slug: str, subject_id: str) -> tuple[dict[str, Any], uuid.UUID]:
    """The assertion for a member known only by DID: their newest submission here.

    For a decision the member took on their own sharing page.
    """
    from sqlalchemy import select

    from celine.onboarding.models.database import async_session
    from celine.onboarding.models.submission import Submission

    async with async_session() as db:
        submission = (
            await db.execute(
                select(Submission)
                .where(Submission.dataspace_did == subject_id, Submission.rec_slug == rec_slug)
                .order_by(Submission.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if submission is None:
        raise AssertionUnavailableError(
            "the member has no application in this community, so no verification backs "
            "an assertion that they hold their supply points"
        )
    return for_submission(submission)


def for_submission(submission: Submission) -> tuple[dict[str, Any], uuid.UUID]:
    row = getattr(submission, "verification", None)
    return build(row), row.id  # build() raised if row is None


async def mark_accepted(verification_id: uuid.UUID) -> None:
    """Record that a holder accepted an assertion citing this verification.

    That is what makes the row one an erasure keeps (REQ-0045). Its own session:
    the callers run outside a request's transaction. A failure is logged loudly
    and not raised — the grant stands at the holder either way, and the holder
    keeps the assertion — but the row would then not be protected.
    """
    from sqlalchemy import update

    from celine.onboarding.models.database import async_session
    from celine.onboarding.models.verification import SubmissionVerification

    try:
        async with async_session() as db:
            await db.execute(
                update(SubmissionVerification)
                .where(SubmissionVerification.id == verification_id)
                .values(last_asserted_at=datetime.now(UTC))
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — see the docstring
        logger.exception(
            "Could not record that verification %s backs a grant; it is not protected "
            "from erasure until the next grant records it",
            verification_id,
        )


# --- what the holder answered -------------------------------------------------

#: ds's 409 details (contract v1.1), by their opening words.
_HELD_ELSEWHERE = "key already held by another subject at this holder"
_SUSPENDED = "key suspended by the holder"


def explain_refusal(status: int, text: str, *, where: str) -> str | None:
    """A sentence for the community's operator, for the refusals R4 added; else None."""
    body = text or ""
    if status == 409 and _HELD_ELSEWHERE in body:
        return (
            f"{where} already has this supply point registered for another person. The "
            "same POD cannot be shared for two people at once. Check who holds the "
            "supply point now; if this member does, the other registration has to be "
            "withdrawn by the community that made it, or by the grid operator."
        )
    if status == 409 and _SUSPENDED in body:
        return (
            f"{where} has suspended this supply point, usually because its contract "
            "holder changed. It is released again only after the community verifies "
            "anew that this member holds it: record a new verification with evidence, "
            "then retry the dataspace share."
        )
    if status == 422 and "key_assertion" in body:
        # pydantic's own refusal of an unknown field: an older connector.
        if "extra_forbidden" in body or "Extra inputs" in body:
            return (
                f"{where} runs a version that does not accept the community's assertion "
                "yet. Switch DS_KEY_ASSERTION off for this deployment until it is "
                "upgraded, or upgrade the connector."
            )
        return (
            f"{where} did not accept the community's assertion that the member holds "
            "this supply point (missing, or verified in a way it does not accept). "
            "Record a verification with evidence — the evidence file, or a document "
            "uploaded with the application — then retry the dataspace share."
        )
    return None


def suspension_line(where: str, offer_id: str, count: int) -> str:
    """What the share step reports for keys a holder suspended (contract v1.1 §1)."""
    return (
        f"{offer_id}: {where} has suspended {count} of the member's supply point"
        f"{'s' if count != 1 else ''} (holder change) — nothing is released for "
        f"{'them' if count != 1 else 'it'} until the community records a new verification "
        "with evidence and retries this step"
    )


# --- posture --------------------------------------------------------------------


def add_posture(guard: PostureGuard, communities: dict[str, list[str]]) -> None:
    """Register what a hardened deployment granting at a holder must not lack.

    ``communities`` is :func:`service_auth.collecting_organisations`'s answer.
    """
    at_holders = sorted(
        slug for slugs in communities.values() for slug in slugs if grants_at_a_holder(slug)
    )
    if not at_holders:
        return
    recs = ", ".join(repr(s) for s in at_holders)
    if settings.ds_key_assertion is False:
        guard.add(
            "DS_KEY_ASSERTION",
            f"is false, and REC {recs} grants members' supply points at another "
            "participant's connector without asserting that they hold them",
            "Unset DS_KEY_ASSERTION (it defaults to on outside dev).",
        )
        return
    key = settings.rec_assertion_hmac_key.strip()
    if not key:
        guard.add(
            "REC_ASSERTION_HMAC_KEY",
            f"is not set, and REC {recs} asserts members' supply points to a holder",
            "Set REC_ASSERTION_HMAC_KEY to a random secret of its own.",
        )
    elif key in {k.strip() for k in settings.encryption_key.split(",")} or key == (
        settings.otp_hmac_key.strip()
    ):
        guard.add(
            "REC_ASSERTION_HMAC_KEY",
            "is the same as ENCRYPTION_KEY or OTP_HMAC_KEY",
            "Give REC_ASSERTION_HMAC_KEY its own random secret.",
        )
