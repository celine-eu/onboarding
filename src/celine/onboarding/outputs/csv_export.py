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

    #: What the audience was computed from: every dataset, at every connector
    #: holding the offer, whose subject set was the same. Empty without a
    #: connector. The header names them (ADR-0008) — a reader cannot otherwise
    #: tell the audience of the whole offer from the audience of one dataset.
    datasets: tuple[dataspace_identity.DatasetAudience, ...] = ()
    consumer_did: str | None = None
    #: Members who withdrew from the offer everywhere they decided on it (plan D6,
    #: ADR-0009). Empty without a connector — the intake records cannot say.
    withdrawals: tuple[dataspace_identity.Withdrawal, ...] = ()
    #: Connectors holding the offer that serve no list of decisions, so whoever
    #: withdrew there is not reported. Named in the header, never implied.
    unreported: tuple[dataspace_identity.ConsentRoute, ...] = ()

    @property
    def routes(self) -> tuple[dataspace_identity.ConsentRoute, ...]:
        """The connectors holding the offer's data."""
        return tuple(dict.fromkeys(d.route for d in self.datasets))


async def _resolve_audience(
    rec_slug: str, offer_id: str
) -> tuple[_Audience, dict | None, frozenset[str] | None]:
    """Decide who is in this export, and say which system decided it.

    Returns the audience description, the offer's published record when one was
    read, and the set of subject DIDs the connectors authorise — ``None`` when
    there is no connector and the local columns decide instead.

    **Read wherever the offer is held.** The connectors come from the REC's
    ``dataspace.connectors`` through :func:`dataspace_identity.consent_routes` —
    the call the grant, withdrawal and retry paths make — so an offer whose data
    sits at another participant's connector is read there, and one held at
    several is read at each and exported only where they agree (ADR-0008).

    **The recipient comes from the offer.** ``recipients.recipient`` — the field
    ds renamed from ``controller``, and the old spelling is still read — is an
    owner alias, and the consent plane is keyed by DID, so the identity registry
    resolves one to the other. Nothing else may name the recipient: the person
    consented to disclosure to the party *this offer* names, and sourcing it from
    a manifest binding or the community's grid operator could hand data to a
    party the offer does not name.
    """
    if not settings.ds_connector_url:
        # A deployment with no dataspace has no connector to ask, and the intake
        # form is then the only record a consent decision has. That fallback is
        # for consent only — it is not a second opinion to prefer when a
        # connector *is* configured and unreachable, which raises instead.
        return _Audience(source="submission"), None, None

    # The routing is in the manifest, so the cache has to be authoritative before
    # it is read — as on every path that routes a consent.
    await template_service.ensure_fresh()
    offer = await template_service.get_sharing_offer(rec_slug, offer_id)
    controller = template_service.offer_recipient(offer)
    if not controller:
        # Required by the connector's own sharing-offers schema, so an offer
        # without one means the published vocabulary is not what this code was
        # written against — not something to guess a recipient for.
        raise ValueError(
            f"Sharing offer {offer_id!r} names no controller, so the party the "
            "consent is read for cannot be determined from the offer the people "
            "consented to."
        )

    consumer_did = await dataspace_identity.resolve_consumer_did(controller)
    routes = dataspace_identity.consent_routes(
        template_service.dataspace_binding(rec_slug), settings.ds_connector_url, [offer_id]
    )
    audience = await dataspace_identity.get_offer_audience(offer_id, consumer_did, routes)
    # Who withdrew, read where the audience was — as the community, whose
    # members' decisions these are — and checked against it (ADR-0009).
    decisions = await dataspace_identity.get_offer_decisions(offer_id, audience.routes)
    withdrawn = dataspace_identity.withdrawals(audience, decisions)
    return (
        _Audience(
            source="connector",
            datasets=audience.datasets,
            consumer_did=consumer_did,
            withdrawals=withdrawn,
            unreported=decisions.unreported,
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
    # The header keeps saying "Controller" although the field is now
    # `recipients.recipient`: the word describes what a reader of this file was
    # told, and renaming it would change what past and future exports call the
    # same party. `recipient_role` is read the same way, old spelling included.
    controller = template_service.offer_recipient(offer)
    role = recipients.get("recipient_role") or recipients.get("controller_role")
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


#: The supply-point list's columns (ADR-0009). **Two columns, not a state flag**:
#: a supply point that stands authorised is only ever in `authorised_pod_code`,
#: one whose member withdrew only ever in `withdrawn_pod_code`. A reader who takes
#: one column cannot pick up the other's rows, and a reader of the old single
#: `pod_code` column fails on the missing name instead of silently reading a
#: withdrawal as an authorisation.
POD_LIST_COLUMNS = ("authorised_pod_code", "withdrawn_pod_code", "withdrawn_at", "withdrawn_by")


def _withdrawn_rows(
    withdrawals: tuple[dataspace_identity.Withdrawal, ...],
    held: dict[str, list[str]],
    *,
    authorised: set[str],
) -> list[dict[str, str]]:
    """One row per supply point of a member who withdrew, sorted by supply point.

    A supply point another member still authorises is left out of this column:
    the holder's data plane filters by supply point, so it *is* served, and
    listing it as withdrawn would contradict the other column. Two withdrawn
    members sharing one keep the later withdrawal.
    """
    by_pod: dict[str, dataspace_identity.Withdrawal] = {}
    for withdrawal in withdrawals:
        for pod in held.get(withdrawal.subject_id, []):
            if pod in authorised:
                continue
            current = by_pod.get(pod)
            if current is None or withdrawal.withdrawn_at > current.withdrawn_at:
                by_pod[pod] = withdrawal
    return [
        {
            "withdrawn_pod_code": pod,
            "withdrawn_at": by_pod[pod].withdrawn_at,
            "withdrawn_by": by_pod[pod].withdrawn_by,
        }
        for pod in sorted(by_pod)
    ]


def _withdrawal_lines(audience: _Audience) -> list[str]:
    """What the two columns mean, and whose withdrawals could not be reported."""
    lines = [
        "# authorised_pod_code: a supply point whose member's consent to this offer "
        "stands, as the consent source above reports it.",
        "# withdrawn_pod_code: a supply point whose member withdrew from this offer — "
        "NOT authorised. withdrawn_at is when (the latest across datasets and holders); "
        "withdrawn_by is whose act it was: subject (the member), collector (this "
        "community), operator (the connector's operator, at the member's request) or "
        "service.",
    ]
    if audience.source != "connector":
        lines.append(
            "# Withdrawals NOT reported: no dataspace connector is configured, and the "
            "intake records do not say who withdrew."
        )
        return lines
    lines.append(
        "# Withdrawals: as each holder lists this community's current members' "
        "decisions. A member never asked, or no longer a member, is in neither column."
    )
    for route in audience.unreported:
        lines.append(
            f"# Withdrawals NOT reported for {route.where}: it serves no list of "
            "decisions, so a member who withdrew there is in neither column."
        )
    return lines


async def _submitted_pods_by_did(
    db: AsyncSession, *, rec_slug: str, dids: set[str]
) -> dict[str, list[str]]:
    """Intake's supply points for these DIDs — the no-registry fallback, per member."""
    result = await db.execute(select(Submission).where(Submission.rec_slug == rec_slug))
    found: dict[str, list[str]] = {}
    for sub in result.scalars().all():
        if sub.dataspace_did in dids and sub.pod_code:
            found.setdefault(str(sub.dataspace_did), []).append(str(sub.pod_code))
    return found


async def export_pod_list(
    db: AsyncSession,
    output_path: str | Path,
    *,
    rec_slug: str,
    offer_id: str,
    generated_at: datetime,
) -> int:
    """Write the supply points whose owners agree, and whose owners withdrew — nothing else.

    **This is the collector's own evidence**: a dated, offer-scoped record of
    which supply points stood authorised under an offer, for the community that
    collected the decisions and has to be able to demonstrate them. It is not how
    a holder learns the list — the holder already has it, because the keys travel
    with each decision to its connector (ADR-0007) and its data plane filters on
    them. The file needs no names, hashes, DIDs or evidence bundles — that
    material belongs in the dataspace, where it is verifiable and revocable, and
    copying it into a second store is how two records of the same consent start
    to disagree. So minimisation is the shape of this command rather than a step
    in a runbook someone skips.

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

    **Who withdrew is reported as withdrawn** (ADR-0009), in its own column,
    read as the community from every holder's decisions list. A withdrawn supply
    point is never under ``authorised_pod_code``; a holder serving no decisions
    list is named in the header as not reported.

    **Checked per dataset, scoped per offer** (ADR-0008). The offer is read at
    every connector holding its data, and each answers per dataset. One distinct
    set of people across all of them is the offer's audience, and the file says
    which datasets and holders it was computed from; more than one is refused,
    naming the split, because no one list would be true of the whole offer.

    **Where there is nothing to ask, the local record still decides.** A
    deployment without a dataspace has no connector holding a consent decision,
    and one without a registry — or a community declaring no ``rec_registry``
    block — has nowhere to ask what a member holds. Both are supported
    configurations, and they are independent: the file names its source for each
    of the two questions, because a reader cannot otherwise tell and the answers
    carry different guarantees.

    **Evidence, not a disclosure** (ADR-0010). Nothing is recorded with a
    connector and nothing is posted anywhere: there is no handover, and a
    ``DataDisclosed`` would assert a release that never happens.

    **For the party the offer names, read from the offer.** The audience is
    computed for the offer's recipient (``recipients.recipient``), resolved to its
    DID by the identity registry, and the header names it. The caller names
    nobody: the file goes to nobody (ADR-0010), so there is no second party to
    compare against.

    **The file is a snapshot.** A decision changed after it was generated is not
    in it, so it says when it was made and that it goes stale. That promise is
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
    #
    # A withdrawn member's supply points are read the same way, in the same call:
    # the file reports them as withdrawn, and "which supply points" has one owner
    # whichever column they land in.
    withdrawn_dids = {w.subject_id for w in audience.withdrawals}
    held = None
    if subject_ids is not None:
        held = await rec_registry.supply_points_by_did(
            sorted(subject_ids | withdrawn_dids), rec_slug=rec_slug
        )

    withdrawn_held: dict[str, list[str]]
    if held is not None:
        audience = replace(audience, pod_source="registry")
        # Deduplicated across members and sorted. `subject_ids` is a frozenset,
        # so neither the request nor the response has a stable order between
        # runs; a list of supply points has no meaningful order of its own, and
        # a stable one is what lets a recipient diff two exports.
        pods = sorted({pod for did, points in held.items() if did in subject_ids for pod in points})
        withdrawn_held = {did: held.get(did, []) for did in withdrawn_dids}
    else:
        pods = await _submitted_pods(
            db, rec_slug=rec_slug, offer_id=offer_id, subject_ids=subject_ids
        )
        withdrawn_held = (
            await _submitted_pods_by_did(db, rec_slug=rec_slug, dids=withdrawn_dids)
            if withdrawn_dids
            else {}
        )
    withdrawn_rows = _withdrawn_rows(audience.withdrawals, withdrawn_held, authorised=set(pods))

    # **Nothing is recorded as a disclosure** (ADR-0010). This file is the
    # community's own dated evidence and is not handed to anyone, so a
    # `DataDisclosed` would assert a release that never happens.

    stamp = generated_at.isoformat()
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        f.write(
            f"# Evidence: which supply points stood authorised under offer {offer_id}, "
            "and which members had withdrawn, at the generation time below.\n"
        )
        f.write(
            "# This community's own record, as the collector of these decisions "
            "(ADR-0010). It is kept here; it gives nobody access to anything.\n"
        )
        f.write(f"# Community: {rec_slug}\n")
        f.write(f"# Generated: {stamp}\n")
        for line in _offer_terms(offer):
            f.write(line + "\n")
        if audience.source == "connector":
            f.write(
                f"# Consent source: the dataspace connectors holding this offer, as of "
                f"the generation time above, for the party the offer names "
                f"({audience.consumer_did}).\n"
            )
            f.write(
                "# Computed from the one dataset bound to this offer:\n"
                if len(audience.datasets) == 1
                else f"# Computed from {len(audience.datasets)} datasets bound to this "
                "offer, every one with this same audience:\n"
            )
            for dataset in audience.datasets:
                f.write(f"#   {dataset.dataset_id}, held by {dataset.held_by}\n")
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
        for line in _withdrawal_lines(audience):
            f.write(line + "\n")
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
        writer = csv.DictWriter(f, fieldnames=list(POD_LIST_COLUMNS), restval="")
        writer.writeheader()
        for pod in pods:
            writer.writerow({"authorised_pod_code": pod})
        for row in withdrawn_rows:
            writer.writerow(row)

    logger.info(
        "Supply-point evidence for %s under %r: %d authorised, %d withdrawn, %d holder(s) "
        "not reporting withdrawals",
        rec_slug,
        offer_id,
        len(pods),
        len(withdrawn_rows),
        len(audience.unreported),
    )
    return len(pods)


async def export_submissions_csv(
    db: AsyncSession,
    output_path: str | Path,
    *,
    rec_slug: str | None = None,
) -> int:
    """The community's register, for the community's own use.

    Every submission, every field, no consent filter. **Not a disclosure path**:
    it takes no recipient, because handing this file to another organisation has
    no basis anywhere in this system — the supply-point list is the governed way
    to give another party anything. Operator access is recorded in the audit log
    by the caller.
    """
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
