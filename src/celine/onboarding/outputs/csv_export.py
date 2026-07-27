import csv
import hashlib
import json
import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.models.submission import Submission
from celine.onboarding.services import template_service

logger = logging.getLogger(__name__)

BASE_FIELDS = [
    "id",
    "ref",
    "status",
    "first_name",
    "last_name",
    "email",
    "phone",
    "phone_verified",
    "phone_verified_at",
    "fiscal_code",
    "pod_code",
    # Consent status with timestamps and versions (3A.2) — needed to filter by
    # who actually consented, and to which document version, before any sharing.
    "gdpr_consent",
    "gdpr_consent_at",
    "gdpr_consent_version",
    "policy_consent",
    "policy_consent_at",
    "policy_consent_version",
    "statute_consent",
    "statute_consent_at",
    "statute_consent_version",
    # Data-sharing consent (Block B) — optional, with the offers, version, locale
    # and rendered-text hash that record what the person actually saw.
    "data_sharing_consent",
    "data_sharing_consent_at",
    "data_sharing_consent_offer_ids",
    "data_sharing_consent_text_version",
    "data_sharing_consent_locale",
    "data_sharing_consent_text_sha256",
    "share_provisioned",
    # Dataspace identity provisioned on approval (3A.1).
    "dataspace_did",
    "dataspace_subject_id",
    "created_at",
    "updated_at",
]


def _fmt(value: object) -> str:
    """Render a cell: None → empty string (not the literal 'None')."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _extra_field_keys(rec_slug: str | None) -> list[str]:
    if not rec_slug:
        keys: list[str] = []
        for slug in template_service.get_slugs():
            manifest = template_service.load_manifest(slug)
            for f in manifest.get("fields", {}).get("extra", []):
                if "key" in f and f["key"] not in keys:
                    keys.append(f["key"])
        return keys
    manifest = template_service.load_manifest(rec_slug)
    return [f["key"] for f in manifest.get("fields", {}).get("extra", []) if "key" in f]


def _disclosure_snapshot_hash(submissions: Sequence[Submission]) -> str:
    """A recomputable, non-PII fingerprint of the consent state in a disclosure.

    Mirrors the connector's ``consent_snapshot_hash`` (SHA-256 over sorted
    tuples) but over what onboarding holds: **offer-level** consent, since
    dataset-level resolution lives in the connector.  Each tuple is
    ``(subject_ref, rec_slug, offer_ids, consent_text_version)`` for submissions
    that opted in — ``subject_ref`` is the pseudonymous dataspace DID (or the
    opaque subject id / submission ref as a fallback), never a name, email, CF or
    POD.
    """
    tuples = sorted(
        (
            sub.dataspace_did or sub.dataspace_subject_id or sub.ref or "",
            sub.rec_slug or "",
            ",".join(sorted(sub.data_sharing_consent_offer_ids or [])),
            sub.data_sharing_consent_text_version or "",
        )
        for sub in submissions
        if sub.data_sharing_consent
    )
    payload = json.dumps(tuples, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def export_pod_list(
    db: AsyncSession,
    output_path: str | Path,
    *,
    rec_slug: str,
    offer_id: str,
    recipient_ref: str,
    generated_at: datetime,
    purpose: list[str] | None = None,
    agreement_ref: str | None = None,
) -> int:
    """Write the list of supply points whose owners agreed, and nothing else.

    The distributor needs the PODs it may hand over. It does not need names,
    hashes, DIDs or evidence bundles — that material belongs in the dataspace,
    where it is verifiable and revocable, and copying it into a second store is
    how two records of the same consent start to disagree. So minimisation is the
    shape of this command rather than a step in a runbook someone skips.

    Filtered on a consent for *this offer*: consent is purpose-scoped, so someone
    who agreed to a different offer has not agreed to this handover.

    **The file is a snapshot, and the re-export cadence is the revocation
    latency.** Somebody who withdraws stays on the recipient's copy until the next
    run, so the file says when it was made and that it goes stale. A register that
    does not announce its own staleness is how a withdrawn consent keeps being
    honoured.
    """
    query = (
        select(Submission)
        .where(Submission.rec_slug == rec_slug)
        .where(Submission.data_sharing_consent.is_(True))
        .where(Submission.share_provisioned.is_(True))
        .order_by(Submission.created_at.asc())
    )
    result = await db.execute(query)

    # `data_sharing_consent_offer_ids` is an encrypted JSON column, so the offer
    # filter cannot be pushed into SQL — it is applied after decryption.
    rows = [
        sub
        for sub in result.scalars().all()
        if offer_id in (sub.data_sharing_consent_offer_ids or []) and sub.pod_code
    ]

    stamp = generated_at.isoformat()
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        f.write(f"# Supply points authorised for release under offer {offer_id}\n")
        f.write(f"# Community: {rec_slug}\n")
        f.write(f"# Generated: {stamp}\n")
        f.write(
            "# This is a snapshot. Consent can be withdrawn at any time, so this "
            "list goes out of date from the moment it is written; use the most "
            "recent export.\n"
        )
        writer = csv.DictWriter(f, fieldnames=["pod_code"])
        writer.writeheader()
        for sub in rows:
            writer.writerow({"pod_code": _fmt(sub.pod_code)})

    try:
        from celine.onboarding.services.dataspace_identity import emit_data_disclosed

        await emit_data_disclosed(
            recipient_ref=recipient_ref,
            purpose=purpose or [],
            columns=["pod_code"],
            subject_count=len(rows),
            source_ref=rec_slug,
            consent_snapshot_hash=_disclosure_snapshot_hash(rows),
            agreement_ref=agreement_ref,
            rec_slug=rec_slug,
        )
    except Exception:
        # Accountability must never block the disclosure it documents; an
        # operator who cannot export stops naming recipients, which loses the
        # trail entirely.
        logger.exception("Failed to emit DataDisclosed provenance for POD list")

    return len(rows)


async def export_submissions_csv(
    db: AsyncSession,
    output_path: str | Path,
    *,
    rec_slug: str | None = None,
    recipient_ref: str | None = None,
    purpose: list[str] | None = None,
    agreement_ref: str | None = None,
) -> int:
    query = select(Submission).order_by(Submission.created_at.desc())
    if rec_slug:
        query = query.where(Submission.rec_slug == rec_slug)
    result = await db.execute(query)
    submissions = result.scalars().all()

    extra_keys = _extra_field_keys(rec_slug)
    fieldnames = BASE_FIELDS + extra_keys

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for sub in submissions:
            row = {field: _fmt(getattr(sub, field, None)) for field in BASE_FIELDS}
            extra = sub.extra_data or {}
            for key in extra_keys:
                row[key] = _fmt(extra.get(key))
            writer.writerow(row)

    # Record the disclosure in ds-provenance, if a recipient was named. Naming a
    # recipient is what makes an export a *disclosure* worth logging; without one
    # (e.g. an internal dump) nothing is emitted. Import is local so the export
    # keeps working in environments with no dataspace integration.
    if recipient_ref:
        try:
            from celine.onboarding.services.dataspace_identity import emit_data_disclosed

            await emit_data_disclosed(
                recipient_ref=recipient_ref,
                purpose=purpose or [],
                columns=list(fieldnames),
                subject_count=len(submissions),
                source_ref=rec_slug,
                consent_snapshot_hash=_disclosure_snapshot_hash(submissions),
                agreement_ref=agreement_ref,
                rec_slug=rec_slug,
            )
        except Exception:
            logger.exception("Failed to emit DataDisclosed provenance for export")

    return len(submissions)
