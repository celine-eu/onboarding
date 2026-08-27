import csv
import logging
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

    # Recorded **before** the file is written. `POST /admin/disclosure` runs ahead
    # of the handover by design, so a refusal means the disclosure does not
    # happen — writing first and recording after is how an unrecorded handover
    # gets out. A failure here raises and no file is produced.
    from celine.onboarding.services.dataspace_identity import record_disclosure

    disclosures = await record_disclosure(
        offer_id=offer_id,
        recipient_ref=recipient_ref,
        purpose=purpose or [],
        columns=["pod_code"],
        subject_count=len(rows),
        source_ref=rec_slug,
        agreement_ref=agreement_ref,
        # Stable across retries of *this* export, so a retry after a partial
        # failure re-records rather than duplicating. The connector derives one
        # key per dataset from it.
        event_id=f"pod-list:{rec_slug}:{offer_id}:{generated_at.isoformat()}",
        rec_slug=rec_slug,
    )

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

    for entry in disclosures:
        logger.info(
            "DataDisclosed recorded for %s: dataset=%s consent_snapshot_hash=%s "
            "granted_parties=%s",
            rec_slug,
            entry.get("dataset_id"),
            entry.get("consent_snapshot_hash"),
            entry.get("granted_party_count"),
        )

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

    # **No dataspace disclosure is recorded here, deliberately.** This exports
    # every submission in the community — no consent filter, no offer, every
    # field including name, fiscal code and POD. There is no offer to resolve and
    # no governed dataset it corresponds to: the datasets governance declares are
    # meter and weather data, not the onboarding database. Filing this under one
    # of them would attach a PII export to a consent state that has nothing to do
    # with it, which is worse than not recording it in the dataspace at all.
    #
    # It is accounted for where it belongs: `POST /{rec_slug}/exports/csv` writes
    # an audit record — actor, IP, row count, named recipient — on every call.
    # If that is not enough, the answer is a stronger admin audit trail, not a
    # `DataDisclosed` event.
    return len(submissions)
