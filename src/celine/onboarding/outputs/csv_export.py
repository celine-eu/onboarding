import csv
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission
from celine.onboarding.services import dataspace_identity, rec_registry, template_service

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


@dataclass(frozen=True, slots=True)
class _Audience:
    """Who the export is for, and which record decided it."""

    source: str
    """``connector`` or ``submission`` — named in the file's own header."""

    pod_source: str = "submission"
    """``registry`` or ``submission``: which system the supply points came from.

    Independent of :attr:`source`, and named separately in the header for that
    reason. The two questions have different owners — the connector holds the
    consent, the registry holds the supply points — and a deployment can be
    configured to answer one from the running system and the other from what
    intake recorded. A header that named only the first would let a reader
    assume the second.
    """

    dataset_id: str | None = None
    consumer_did: str | None = None
    controller: str | None = None


async def _resolve_audience(
    rec_slug: str, offer_id: str
) -> tuple[_Audience, dict | None, frozenset[str] | None]:
    """Decide who is in this export, and say which system decided it.

    Returns the audience description, the offer's published record when one was
    read, and the set of subject DIDs the connector authorises — ``None`` when
    there is no connector and the local columns decide instead.

    **The recipient comes from the offer.** ``recipients.controller`` is an owner
    alias and the consent plane is keyed by DID, so the identity registry
    resolves one to the other. Nothing else may name the recipient: the person
    consented to disclosure to the controller *this offer* names, and sourcing it
    from a manifest binding or the community's grid operator could hand data to a
    party the offer does not name.
    """
    if not settings.ds_connector_url:
        # A deployment with no dataspace has no connector to ask, and the intake
        # form is then the only record a consent decision has. That fallback is
        # for consent only — it is not a second opinion to prefer when a
        # connector *is* configured and unreachable, which raises instead.
        return _Audience(source="submission"), None, None

    offer = await template_service.get_sharing_offer(rec_slug, offer_id)
    controller = str((offer.get("recipients") or {}).get("controller") or "").strip()
    if not controller:
        # Required by the connector's own sharing-offers schema, so an offer
        # without one means the published vocabulary is not what this code was
        # written against — not something to guess a recipient for.
        raise ValueError(
            f"Sharing offer {offer_id!r} names no controller, so the recipient of "
            "this disclosure cannot be determined from the offer the people "
            "consented to."
        )

    consumer_did = await dataspace_identity.resolve_consumer_did(controller)
    audience = await dataspace_identity.get_offer_audience(offer_id, consumer_did)
    return (
        _Audience(
            source="connector",
            dataset_id=audience.dataset_id,
            consumer_did=consumer_did,
            controller=controller,
        ),
        offer,
        audience.subject_ids,
    )


def _offer_terms(offer: dict | None) -> list[str]:
    """The offer's own terms, for the file header.

    Taken from the connector's published projection — the same facts the person
    was shown when they decided — rather than collected during intake. Coverage
    and retention are properties of *what was consented to*, uniform across
    everyone who accepted the offer; asking each person for them separately
    would create a second record of one fact, which is the failure this export
    is being repaired to stop making.
    """
    if not offer:
        return []
    lines: list[str] = []
    recipients = offer.get("recipients") or {}
    controller = recipients.get("controller")
    role = recipients.get("controller_role")
    if controller:
        lines.append(f"# Controller: {controller}" + (f" ({role})" if role else ""))
    if offer.get("purpose"):
        lines.append(f"# Purpose: {offer['purpose']}")
    coverage = offer.get("coverage") or {}
    if coverage.get("retrospective") or coverage.get("prospective"):
        lines.append(
            "# Coverage: "
            f"retrospective {coverage.get('retrospective') or '-'}, "
            f"prospective {coverage.get('prospective') or '-'}"
        )
    if offer.get("resolution"):
        lines.append(f"# Resolution: {offer['resolution']}")
    if offer.get("measures"):
        lines.append(f"# Measures: {', '.join(str(m) for m in offer['measures'])}")
    if offer.get("retention"):
        lines.append(f"# Retention: {offer['retention']}")
    return lines


async def _submitted_pods(
    db: AsyncSession,
    *,
    rec_slug: str,
    offer_id: str,
    subject_ids: frozenset[str] | None,
) -> list[str]:
    """The supply points as intake recorded them — the fallback, not the source.

    Reached in two configurations, and in both because there is nothing better to
    read. With no connector there is no record of a consent decision but this
    one; with no registry — or a community that declares no ``rec_registry``
    block — there is nowhere to ask what a member holds. Both are supported
    deployments, and the file's header says which of the two wrote it rather than
    letting a reader assume the running system was consulted.

    ``subject_ids`` still decides *who* when the connector answered, so a
    deployment with a connector and no registry keeps the consent fix and loses
    only the supply-point half.
    """
    query = (
        select(Submission)
        .where(Submission.rec_slug == rec_slug)
        .order_by(Submission.created_at.asc())
    )
    if subject_ids is None:
        query = query.where(Submission.data_sharing_consent.is_(True)).where(
            Submission.share_provisioned.is_(True)
        )
    result = await db.execute(query)

    if subject_ids is None:
        # `data_sharing_consent_offer_ids` is an encrypted JSON column, so the
        # offer filter cannot be pushed into SQL — it is applied after
        # decryption.
        rows = [
            sub
            for sub in result.scalars().all()
            if offer_id in (sub.data_sharing_consent_offer_ids or []) and sub.pod_code
        ]
    else:
        # `share_provisioned` is deliberately not consulted here. It is this
        # service's memory of a call it made, which is a legitimate thing to
        # keep, but the connector's answer already accounts for whether the
        # consent reached it — and a row it reports while the local flag is
        # false is a person whose consent is real and whose provisioning record
        # is stale, not a person to leave out.
        rows = [
            sub
            for sub in result.scalars().all()
            if sub.dataspace_did and sub.dataspace_did in subject_ids and sub.pod_code
        ]

    return [str(sub.pod_code) for sub in rows]


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

    **The running system decides what goes in it, not the intake form.** Two
    questions, two owners, and a ``Submission`` answers neither: the connector
    holds who currently consents, and the registry holds which supply points they
    hold. A submission records what somebody agreed to on one afternoon, and
    stops being true the moment anything else changes it.

    *Who* — the participant webapp owns the ongoing decision and writes it to the
    connector, and nothing writes back here, so reading the three local columns
    left a person who granted afterwards **out** of the export and a person who
    withdrew afterwards **in**. The second is a disclosure against a withdrawn
    consent.

    *What they hold* — ``Member.delivery_points`` is what the community records
    now, and a POD an operator corrected there has never reached this database.
    Reading the registry also answers for a participant this service never
    registered: an imported member consents through the same offer and was
    silently absent from every export.

    Filtered on a consent for *this offer*: consent is purpose-scoped, so someone
    who agreed to a different offer has not agreed to this handover. The
    connector enforces that server-side by keying its answer on the offer.

    **Where there is nothing to ask, the local record still decides.** A
    deployment without a dataspace has no connector holding a consent decision,
    and one without a registry — or a community declaring no ``rec_registry``
    block — has nowhere to ask what a member holds. Both are supported
    configurations, and they are independent: the file names its source for each
    of the two questions, because a reader cannot otherwise tell and the answers
    carry different guarantees.

    **The file is a snapshot, and the re-export cadence is the revocation
    latency.** Somebody who withdraws stays on the recipient's copy until the next
    run, so the file says when it was made and that it goes stale. That promise is
    only true when the list comes from the connector — a re-export against the
    local columns reproduces the same staleness every time, because the staleness
    is in the source rather than in the snapshot — so the header says so only
    when it holds.
    """
    audience, offer, subject_ids = await _resolve_audience(rec_slug, offer_id)

    # **The registry says what they hold.** `Submission.pod_code` is what one
    # person typed into a form on one afternoon; `Member.delivery_points` is what
    # the community records now, and the two stop agreeing the moment a REC
    # manager corrects one of them. The registry is also the only source that can
    # answer for a participant this service never registered — somebody imported
    # by the REC manager consents through the same offer and was silently absent
    # from every export, which is the same defect as reading the form, one step
    # further along.
    #
    # Keyed on the DID, so it can only be asked where the connector answered.
    # A deployment with no dataspace mints no DIDs, and there is no other join:
    # `Member.user_id` is a Keycloak username and `assets-by-user-ids` answers
    # assets, which is empty for every participant whose meter is not yet
    # installed.
    held = None
    if subject_ids is not None:
        held = await rec_registry.supply_points_by_did(sorted(subject_ids), rec_slug=rec_slug)

    if held is not None:
        audience = replace(audience, pod_source="registry")
        # Deduplicated across members and sorted. `subject_ids` is a frozenset,
        # so neither the request nor the response has a stable order between
        # runs; a list of supply points has no meaningful order of its own, and
        # a stable one is what lets a recipient diff two exports.
        pods = sorted({pod for points in held.values() for pod in points})
    else:
        pods = await _submitted_pods(
            db, rec_slug=rec_slug, offer_id=offer_id, subject_ids=subject_ids
        )

    # The offer is the authority on its own purpose, the same way it is on its
    # controller — an explicit argument still wins, so a caller recording a
    # narrower purpose than the offer declares is not overruled.
    if not purpose and offer and offer.get("purpose"):
        purpose = [str(offer["purpose"])]

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
        # The count of what is actually in the file. The audience read reports
        # its own count per dataset, and it is deliberately not forwarded here:
        # `subject_count` on a `DataDisclosed` describes the handover, and a
        # subject who consents but holds no POD in this community is in the
        # audience and not in the export.
        subject_count=len(pods),
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
        for line in _offer_terms(offer):
            f.write(line + "\n")
        if audience.source == "connector":
            f.write(
                f"# Consent source: the dataspace connector, as of the generation "
                f"time above (recipient {audience.consumer_did}).\n"
            )
            f.write(
                "# This is a snapshot. Consent can be withdrawn at any time, so "
                "this list goes out of date from the moment it is written; use "
                "the most recent export.\n"
            )
        else:
            f.write(
                "# Consent source: this community's intake records. No dataspace "
                "connector is configured, so a decision changed after intake is "
                "not reflected here and re-exporting will not pick it up.\n"
            )
        if audience.pod_source == "registry":
            f.write(
                "# Supply points: this community's registry record, as of the "
                "generation time above.\n"
            )
        else:
            f.write(
                "# Supply points: as declared at onboarding. They have not been "
                "read back from the registry, so a supply point corrected or "
                "retired since is not reflected here.\n"
            )
        writer = csv.DictWriter(f, fieldnames=["pod_code"])
        writer.writeheader()
        for pod in pods:
            writer.writerow({"pod_code": pod})

    for entry in disclosures:
        logger.info(
            "DataDisclosed recorded for %s: dataset=%s consent_snapshot_hash=%s granted_parties=%s",
            rec_slug,
            entry.get("dataset_id"),
            entry.get("consent_snapshot_hash"),
            entry.get("granted_party_count"),
        )

    return len(pods)


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
