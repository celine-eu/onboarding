from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from celine.sdk.auth import OidcClientCredentialsProvider

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission
from celine.onboarding.models.verification import CREDENTIAL_METHOD_PREFIX
from celine.onboarding.services import sharing_intent, template_service
from celine.onboarding.services.service_auth import (
    organisation_auth_headers,
    service_token_provider,
)

logger = logging.getLogger(__name__)

_token_provider: OidcClientCredentialsProvider | None = None

_KC_SYNC_MAX_RETRIES = 3


def _get_token_provider() -> OidcClientCredentialsProvider:
    """This service's own service account, built once in `services.service_auth`.

    The module-level handle stays here because it is what a test substitutes.
    """
    global _token_provider
    if _token_provider is None:
        _token_provider = service_token_provider()
    return _token_provider


def new_subject_id() -> str:
    """A subject id for somebody the registry has never mapped: a random UUIDv4.

    **Derived from nothing, so it reveals nothing** (ds `D-22c`, which binds
    whoever generates the id). It becomes the ``<id>`` of the person's DID
    verbatim and travels in every consent record, provenance event and
    credential that names them.

    **One-shot, and that is the caller's burden.** Unlike the HMAC the registry
    used to derive, calling this again does not give the same answer, so an id
    that is minted and not recorded is an id lost — the next attempt would give
    the same person a second DID. Every caller reuses an existing mapping first
    (:func:`resolve_subject`), and the funnel records the id on the submission
    before issuing (:func:`provision_user_identity`).
    """
    return str(uuid.uuid4())


def _parse_generated_at(value: Any) -> datetime:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC)


async def _auth_headers() -> dict[str, str]:
    token = await _get_token_provider().get_token()
    return {"Authorization": f"Bearer {token.access_token}"}


@dataclass(frozen=True, slots=True)
class OwnerCheck:
    """What the registry said about a bound organisation.

    found keeps the three-way answer this check has always given, and the
    three-way is the point: *no such owner* is a configuration error worth
    refusing to start on, while *registry unreachable* is not — coupling boot to
    another service's availability would turn a transient outage into an outage
    here. A 403 belongs with unreachable, not with unknown.

    status is the owner's lifecycle state — verified, suspended,
    revoked — and is None when the owner was not found, or when the
    registry did not report one.
    """

    found: bool | None
    status: str | None = None
    did: str | None = None
    """The owner's dataspace identifier, when the registry published one.

    Carried because ``/owners/resolve`` already returns it and the POD export
    needs exactly this mapping: a sharing offer names its controller by *alias*,
    and the consent plane is keyed by *DID*. The registry is the only place that
    mapping exists, so reading it here is what stops a second one being invented
    somewhere else.

    None when the owner was not found, when the registry did not report one, or
    when the owner has not been onboarded into the dataspace yet.
    """

    id: str | None = None
    """The owner's own identifier — never one of its aliases.

    ``/owners/resolve`` answers an alias and an id alike, so this is the only way
    a caller can tell which one it was given. Aliases exist so governance files
    written for other deployments resolve here; a record that names a party
    names it by this.
    """


async def check_organization(org_alias: str) -> OwnerCheck:
    """Resolve *org_alias* in the identity registry and report what it is."""
    if not settings.identity_registry_url:
        return OwnerCheck(found=None)
    base_url = settings.identity_registry_url.rstrip("/")
    try:
        headers = await _auth_headers()
        async with httpx.AsyncClient(timeout=10) as client:
            # `/owners/resolve`, not `/admin/owners/{alias}`. The latter matches on
            # `Owner.id`; an alias 404s there, and this function reported that as
            # "no such organisation" — a startup refusal for a deployment that was
            # configured correctly. The registry added this route for exactly this
            # caller and does the id-then-alias fallback itself, so the fallback is
            # not reimplemented here.
            resp = await client.get(
                f"{base_url}/owners/resolve",
                params={"alias": org_alias},
                headers=headers,
            )
    except Exception:
        logger.warning(
            "Could not reach the identity registry to verify organization %r",
            org_alias,
        )
        return OwnerCheck(found=None)
    if resp.status_code == 404:
        return OwnerCheck(found=False)
    if resp.status_code >= 400:
        logger.warning(
            "Identity registry answered %s verifying organization %r",
            resp.status_code,
            org_alias,
        )
        return OwnerCheck(found=None)
    try:
        body = resp.json()
        status = str(body.get("status") or "").strip() or None
        did = str(body.get("did") or "").strip() or None
        owner_id = str(body.get("id") or "").strip() or None
    except ValueError:
        # Found, but the body was not readable. Do not invent a status: an
        # absent one must not read as "not verified" and refuse boot.
        status = None
        did = None
        owner_id = None
    return OwnerCheck(found=True, status=status, did=did, id=owner_id)


async def resolve_consumer_did(controller_alias: str) -> str:
    """The dataspace identifier of the party a sharing offer names as controller.

    A sharing offer names its controller by **alias** — ``grid-operator``,
    ``example-org`` — and the connector's consent plane is keyed by **DID**. The
    identity registry holds the only mapping between the two, and it is already
    reachable from here: this is the same ``/owners/resolve`` call
    :func:`check_organization` makes at boot, read for a different field.

    **The alias comes from the offer, never from the community.** The person
    consented to disclosure to the controller *that offer names*; taking the
    recipient from anywhere else — a manifest binding, the REC's grid operator —
    can hand data to a party the offer does not name, which is a disclosure
    against a consent nobody gave.

    Raises ``ValueError`` when the mapping cannot be made, because every reason
    it cannot is a deployment configuration an operator can fix, and refusing is
    the only safe answer: the connector would accept a wrong-but-plausible DID
    and return an audience for it.
    """
    check = await check_organization(controller_alias)
    if check.found is None:
        raise RuntimeError(
            f"The identity registry could not be reached to resolve controller "
            f"{controller_alias!r}, so the recipient of this disclosure is "
            "unknown and it must not proceed."
        )
    if not check.found:
        raise ValueError(
            f"The sharing offer names controller {controller_alias!r}, which the "
            "identity registry does not know. Register the owner before "
            "exporting under this offer."
        )
    if not check.did:
        raise ValueError(
            f"Controller {controller_alias!r} is registered but holds no "
            "dataspace identifier, so the consent plane has no key to answer "
            "for it. Onboard the owner into the dataspace — mint its DID and "
            "record it on the owner — before exporting under this offer."
        )
    return check.did


@dataclass(frozen=True, slots=True)
class DatasetAudience:
    """Who currently consents to one offer, for one recipient, on one dataset at one connector.

    The unit consent is stored and enforced at: ds keys a consent row on
    ``(dataset_id, offer_id)`` and each connector answers for its own datasets.
    ``subject_ids`` are dataspace DIDs, which is what makes this joinable against
    ``Submission.dataspace_did``.

    Per dataset *and* per route because that is the granularity everything else
    about the offer is answered at too — who withdrew included
    (:class:`DecisionCell`, read by :func:`get_offer_decisions` from the same
    connectors).
    """

    dataset_id: str
    route: ConsentRoute
    subject_ids: frozenset[str]
    #: The connector's own count, kept beside the set it was taken from rather
    #: than recomputed: they agreeing is worth being able to assert, and they are
    #: two different claims.
    subject_count: int

    @property
    def held_by(self) -> str:
        """The participant holding this dataset: another's alias, or this community's own."""
        return self.route.holder or self.route.collector or "this community"


@dataclass(frozen=True, slots=True)
class OfferAudience:
    """Who currently consents to one offer, for one recipient — one set, read everywhere it is held.

    Only ever built by :func:`get_offer_audience` once every dataset at every
    connector holding the offer has answered with **the same** subject set, so
    :attr:`subject_ids` is the offer's audience and not a choice among several.
    ``datasets`` stays per dataset so a reader — the file's header, a log line —
    can say what that set was computed from.
    """

    offer_id: str
    datasets: tuple[DatasetAudience, ...]

    @property
    def subject_ids(self) -> frozenset[str]:
        return self.datasets[0].subject_ids

    @property
    def routes(self) -> tuple[ConsentRoute, ...]:
        """The connectors that hold a dataset for the offer, once each, in the order asked."""
        return tuple(dict.fromkeys(d.route for d in self.datasets))


class AudienceSplitError(ValueError):
    """An offer's datasets do not agree on who consents, so no one list is its audience."""


async def _audience_at(
    client: httpx.AsyncClient,
    route: ConsentRoute,
    headers: dict[str, str],
    *,
    offer_id: str,
    consumer_id: str,
) -> list[DatasetAudience] | str:
    """One connector's answer: its datasets for the offer, or why it holds none.

    A ``str`` is that connector saying it holds no dataset for the offer (its
    ``422``) — an answer, not a failure, and the caller decides what it means
    across every route.
    """
    try:
        resp = await client.get(
            f"{route.connector_url.rstrip('/')}/consent/admin/shares",
            params={"offer_id": offer_id, "consumer_id": consumer_id},
            headers=headers,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"{route.where} could not be reached to read the audience for offer "
            f"{offer_id!r}, so who consents is unknown and the export must not "
            f"proceed: {exc}"
        ) from exc

    if resp.status_code == 409:
        # Not consent-based: an offer that is disclosed, not consented, has no
        # audience anywhere, and this is a property of the offer.
        raise ValueError(
            f"Offer {offer_id!r} is not consent-based, so it has no audience to "
            f"export ({route.where} answered 409): {resp.text}"
        )
    if resp.status_code == 422:
        # The connector knows no dataset for the offer here. For a route the
        # manifest names that is the one expected refusal: this connector does
        # not hold the offer's data. (The wildcard-consumer 422 is unreachable
        # from here — the consumer is always a resolved DID.)
        return f"{route.where} answered 422: {resp.text}"
    if resp.status_code >= 400:
        raise RuntimeError(
            f"{route.where} answered {resp.status_code} reading the audience for "
            f"offer {offer_id!r}, so who consents is unknown and the export must "
            f"not proceed: {resp.text}"
        )

    try:
        body = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"The audience {route.where} reported for offer {offer_id!r} was not "
            "readable, so who consents is unknown and the export must not proceed."
        ) from exc

    datasets = body.get("datasets") or []
    if not datasets:
        # Distinct from "nobody consents" and from "holds nothing here": the
        # connector refuses a dataset-less offer with a 422, so an empty list
        # with a 200 is not the shape this caller was written against.
        raise RuntimeError(
            f"{route.where} reported no dataset for offer {offer_id!r} with a 200; "
            "refusing to export against an audience that describes nothing."
        )
    return [
        DatasetAudience(
            dataset_id=str(entry.get("dataset_id") or ""),
            route=route,
            subject_ids=frozenset(str(s) for s in (entry.get("subject_ids") or [])),
            subject_count=int(entry.get("subject_count") or 0),
        )
        for entry in datasets
    ]


def _split(offer_id: str, datasets: list[DatasetAudience]) -> str:
    """Say which datasets split and by how much, without naming who."""
    groups: dict[frozenset[str], list[DatasetAudience]] = {}
    for dataset in datasets:
        groups.setdefault(dataset.subject_ids, []).append(dataset)

    def _subjects(n: int) -> str:
        return f"{n} subject" + ("" if n == 1 else "s")

    several_holders = len({d.held_by for d in datasets}) > 1
    described = "; ".join(
        f"{_subjects(len(ids))}: "
        + ", ".join(
            f"{d.dataset_id} (held by {d.held_by})" if several_holders else d.dataset_id
            for d in members
        )
        for ids, members in sorted(groups.items(), key=lambda g: -len(g[0]))
    )
    union = frozenset().union(*groups)
    common = frozenset.intersection(*groups)
    differing = len(union - common)
    return (
        f"Offer {offer_id!r}'s datasets do not agree on who consents: "
        f"{len(groups)} distinct audiences across {len(datasets)} datasets — {described}. "
        f"{_subjects(differing)} {'is' if differing == 1 else 'are'} in some of these "
        "audiences and not in others. The offer's statement is no longer true of all "
        "its datasets, so no single list is its audience, and nothing was exported."
    )


async def get_offer_audience(
    offer_id: str, consumer_id: str, routes: Sequence[ConsentRoute]
) -> OfferAudience:
    """Ask every connector holding *offer_id* who currently consents to it for *consumer_id*.

    The read counterpart to :func:`provision_user_shares`, and the reason the
    POD export can stop reading a form. A ``Submission`` records what somebody
    agreed to on one afternoon; the connector holds the decision as it stands
    now, including one made or withdrawn in the participant webapp afterwards.

    **``routes`` are every connector holding the offer's data** — from
    :func:`consent_routes`, the call every consent write makes, never
    ``DS_CONNECTOR_URL`` alone. An offer's data may sit at another participant's
    connector, or at several (ADR-0007), and each answers only for the datasets
    it holds. A route that answers ``422`` holds no dataset for the offer and
    contributes nothing; if none holds one, there is no audience to report and
    that is refused, naming what each connector said.

    **The purpose and controller role are not sent, and must not be.** The
    connector stamps them from the offer, the same way
    ``POST /consent/admin/shares`` and ``POST /admin/disclosure`` already do. A
    caller that cannot supply a purpose cannot omit one, which is what makes the
    under-specification that answers "nobody" on the connector's internal check
    unreachable from here.

    **``consumer_id`` is required and is never the wildcard.** The standing
    rows this service writes are wildcard-scoped, and a per-party opt-out beats
    the standing wildcard. Asking as the wildcard would read only the standing
    rows and return people who have specifically opted out of *this* recipient —
    a disclosure against a withdrawn consent, which is the thing the whole
    change exists to prevent. The connector refuses it; naming the recipient is
    this caller's part of that.

    **The check is per dataset, the audience is per offer** (ADR-0008). Each
    connector answers one subject set per dataset and deliberately does not
    flatten them, because consent is stored and enforced per dataset — a member
    can be in one dataset's audience and not another's, when a dataset was bound
    to the offer after they decided, or when they decided on one dataset alone.
    Reading coarser than that would invent authorisation. But what a member was
    asked is the offer — its purpose, recipient, period and consent text — and no
    clause of that sentence is a dataset. So across every route and every
    dataset: **one distinct set is the offer's audience**, and more than one
    means the offer's statement is no longer true of all its datasets, which is
    refused with :class:`AudienceSplitError` naming the datasets that split and
    by how much. Never a union — someone who withdrew from one dataset would
    appear in a list read as authorisation — and never an intersection, which
    empties silently the moment a dataset nobody was asked about is bound.
    """
    if not routes:
        raise RuntimeError(
            f"No connector was named to read offer {offer_id!r} from, so who "
            "consents cannot be asked."
        )

    headers = await _auth_headers()
    datasets: list[DatasetAudience] = []
    holds_nothing: list[str] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for route in routes:
            answer = await _audience_at(
                client, route, headers, offer_id=offer_id, consumer_id=consumer_id
            )
            if isinstance(answer, str):
                holds_nothing.append(answer)
            else:
                datasets.extend(answer)

    if not datasets:
        raise ValueError(
            f"No connector this community routes offer {offer_id!r} to holds a "
            f"dataset for it ({'; '.join(holds_nothing)}). Check which connectors "
            "hold its data in the REC's dataspace.connectors."
        )
    if holds_nothing:
        logger.warning(
            "Offer %r is routed to a connector that holds no dataset for it; the "
            "audience is read from the others: %s",
            offer_id,
            "; ".join(holds_nothing),
        )

    if len({d.subject_ids for d in datasets}) > 1:
        raise AudienceSplitError(_split(offer_id, datasets))

    return OfferAudience(offer_id=offer_id, datasets=tuple(datasets))


#: `limit` for `GET /consent/admin/decisions` — ds's maximum, so the fewest pages.
_DECISIONS_PAGE = 100
#: A list this long is not a list of one community's members. It stops a
#: connector that keeps handing back a cursor from holding an export forever.
_DECISIONS_MAX_PAGES = 10_000


@dataclass(frozen=True, slots=True)
class DecisionCell:
    """One member's presented decision on an offer, over one dataset, at one connector.

    ds's ``GET /consent/admin/decisions`` row (ADR-0021 there), minus the keys,
    which this service registered itself and never reads back into anything.
    """

    dataset_id: str
    route: ConsentRoute
    state: str  # "granted" | "withdrawn"
    decided_by: str
    decided_at: str | None = None
    revoked_at: str | None = None

    @property
    def held_by(self) -> str:
        return self.route.holder or self.route.collector or "this community"


@dataclass(frozen=True, slots=True)
class OfferDecisions:
    """Every decision this community's members hold on one offer, at every connector asked.

    ``unreported`` are the connectors that serve no decisions list at all — an
    older connector — so whoever withdrew *there* is not in ``cells``. Named, so
    that a reader is told, never left to infer it from an absence.
    """

    offer_id: str
    cells: dict[str, tuple[DecisionCell, ...]]
    unreported: tuple[ConsentRoute, ...] = ()


@dataclass(frozen=True, slots=True)
class Withdrawal:
    """A member who withdrew from the offer everywhere they decided on it."""

    subject_id: str
    #: When the withdrawal stood everywhere: the latest ``revoked_at`` across cells.
    withdrawn_at: str
    #: Whose act it was, ds's code — ``subject``, ``collector``, ``operator``,
    #: ``service`` — joined with ``;`` when the cells disagree.
    withdrawn_by: str


async def _decisions_at(
    client: httpx.AsyncClient, route: ConsentRoute, *, offer_id: str
) -> list[tuple[str, DecisionCell]] | None:
    """One connector's decisions for the offer, every page, as ``(subject, cell)``.

    ``None`` when the connector serves no decisions list at all.
    """
    headers = await organisation_auth_headers(route.collector)
    cells: list[tuple[str, DecisionCell]] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(_DECISIONS_MAX_PAGES):
        params = {"offer_id": offer_id, "limit": str(_DECISIONS_PAGE)}
        if cursor is not None:
            params["cursor"] = cursor
        try:
            resp = await client.get(
                f"{route.connector_url.rstrip('/')}/consent/admin/decisions",
                params=params,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"{route.where} could not be reached to read who withdrew from offer "
                f"{offer_id!r}, so the export must not proceed: {exc}"
            ) from exc

        if resp.status_code in (404, 405) and cursor is None:
            # No such route: a connector older than the decisions list. Not an
            # error about this offer — an absence of the capability — and the
            # caller says so rather than reading it as "nobody withdrew".
            return None
        if resp.status_code >= 400:
            # Everything else, the 422 included: this is only asked of a
            # connector that has just reported datasets for the offer, so "holds
            # nothing here" would contradict it. A 403 is ds saying this
            # community is not an accepted collector there — never an empty list.
            raise RuntimeError(
                f"{route.where} answered {resp.status_code} listing decisions on offer "
                f"{offer_id!r}, so who withdrew is unknown and the export must not "
                f"proceed: {resp.text}"
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise RuntimeError(
                f"The decisions {route.where} listed for offer {offer_id!r} were not readable."
            ) from exc

        for subject in body.get("subjects") or []:
            subject_id = str(subject.get("subject_id") or "")
            for row in subject.get("decisions") or []:
                cells.append(
                    (
                        subject_id,
                        DecisionCell(
                            dataset_id=str(row.get("dataset_id") or ""),
                            route=route,
                            state=str(row.get("state") or ""),
                            decided_by=str(row.get("decided_by") or ""),
                            decided_at=row.get("decided_at"),
                            revoked_at=row.get("revoked_at"),
                        ),
                    )
                )
        # A page may be short, even empty, and not the last: only a null
        # cursor ends the list.
        cursor = body.get("next_cursor")
        if cursor is None:
            return cells
        if cursor in seen:
            raise RuntimeError(
                f"{route.where} handed back a cursor it had already issued while listing "
                f"decisions on offer {offer_id!r}; refusing to read it as complete."
            )
        seen.add(cursor)
    raise RuntimeError(
        f"{route.where} did not finish listing decisions on offer {offer_id!r} within "
        f"{_DECISIONS_MAX_PAGES} pages; refusing to read it as complete."
    )


async def get_offer_decisions(offer_id: str, routes: Sequence[ConsentRoute]) -> OfferDecisions:
    """What this community's members decided on *offer_id*, granted and withdrawn, everywhere.

    The other half of the evidence the POD export is (plan D6, ADR-0009). The
    audience read lists standing grants only — ds keeps it that way so that a
    reader of it can never mistake a withdrawn member for a present one — so a
    member who withdrew is absent from it, and absent is also what a member
    nobody asked looks like. ``GET /consent/admin/decisions`` (ds ADR-0021) lists
    the decision itself.

    **As the community, not as this service.** The route is bounded like the
    per-subject read-back: the organisation that collected the decisions, with
    its own client (``svc-ds-connector-<alias>``), for its own current members
    only. The service client the audience read uses is refused there.

    ``routes`` are the connectors holding the offer's datasets —
    :attr:`OfferAudience.routes` — each asked for the rows it holds, every page
    to the end. A connector with no such route (``404``/``405``, an older ds) is
    named in :attr:`OfferDecisions.unreported`; any other failure raises, because
    who withdrew is then unknown, which is not the same as nobody withdrawing.
    """
    cells: dict[str, list[DecisionCell]] = {}
    unreported: list[ConsentRoute] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for route in routes:
            answer = await _decisions_at(client, route, offer_id=offer_id)
            if answer is None:
                logger.warning(
                    "%s serves no decisions list; withdrawals on offer %r are not reported there",
                    route.where,
                    offer_id,
                )
                unreported.append(route)
                continue
            for subject_id, cell in answer:
                cells.setdefault(subject_id, []).append(cell)
    return OfferDecisions(
        offer_id=offer_id,
        cells={subject: tuple(found) for subject, found in cells.items()},
        unreported=tuple(unreported),
    )


def withdrawals(audience: OfferAudience, decisions: OfferDecisions) -> tuple[Withdrawal, ...]:
    """The members who withdrew, checked against the audience they are absent from.

    **The same rule as the audience, applied to the decisions** (ADR-0009). A
    member whose decisions are all ``withdrawn`` withdrew from the offer. A member
    granted in some datasets and withdrawn in others is the D4 split — the offer's
    statement is not true of all its datasets for them — and is refused with
    :class:`AudienceSplitError`, naming the datasets, never the member. A member
    who never decided is in neither list: absent, as ds reports them.

    **The two reads must not contradict each other.** A member the audience
    authorises and whose every decision is ``withdrawn`` cannot be written in
    either column without stating one read as fact over the other, so that is
    refused too. The reverse — granted everywhere and still not in the audience,
    as a per-recipient opt-out does — is no withdrawal and is not listed.
    """
    authorised = audience.subject_ids
    split: dict[str, tuple[DecisionCell, ...]] = {}
    contradicted = 0
    found: list[Withdrawal] = []
    for subject_id in sorted(decisions.cells):
        cells = decisions.cells[subject_id]
        states = {c.state for c in cells}
        if states == {"granted"}:
            continue
        if states != {"withdrawn"}:
            split[subject_id] = cells
            continue
        if subject_id in authorised:
            contradicted += 1
            continue
        found.append(
            Withdrawal(
                subject_id=subject_id,
                withdrawn_at=str(
                    max(
                        cells, key=lambda c: _instant(c.revoked_at or c.decided_at) or _NEVER
                    ).revoked_at
                    or ""
                ),
                withdrawn_by=";".join(sorted({c.decided_by for c in cells if c.decided_by})),
            )
        )

    if split:
        raise AudienceSplitError(_decisions_split(audience.offer_id, split))
    if contradicted:
        raise ValueError(
            f"Offer {audience.offer_id!r}: {contradicted} "
            + ("subject is" if contradicted == 1 else "subjects are")
            + " authorised by the audience the connectors report and withdrawn in "
            "every decision they list. The two reads disagree, so neither column would "
            "be true, and nothing was exported."
        )
    return tuple(found)


def _decisions_split(offer_id: str, split: dict[str, tuple[DecisionCell, ...]]) -> str:
    """Name the datasets a member is granted and withdrawn in, and how many members."""
    every = [c for cells in split.values() for c in cells]
    several_holders = len({c.held_by for c in every}) > 1

    def label(c: DecisionCell) -> str:
        return f"{c.dataset_id} (held by {c.held_by})" if several_holders else c.dataset_id

    def tally(state: str) -> str:
        counts: dict[str, int] = {}
        for c in every:
            if c.state == state:
                counts[label(c)] = counts.get(label(c), 0) + 1
        return ", ".join(f"{name}: {n}" for name, n in sorted(counts.items()))

    n = len(split)
    return (
        f"Offer {offer_id!r}'s datasets do not agree on who consents: {n} "
        + ("subject is" if n == 1 else "subjects are")
        + f" granted in some datasets and withdrawn in others — withdrawn in "
        f"{tally('withdrawn')}; granted in {tally('granted')}. The offer's statement is "
        "no longer true of all its datasets, so no single list is its audience, and "
        "nothing was exported."
    )


# **How the person was checked, and by whom.** Both doors — the onboarding funnel
# and the participant wizard — record the same value, because the operator's answer
# on 2026-09-06 was that they *are* the same check: "same checks, but one outside
# onboarding, the other is the user doing it in onboarding." The pair records the
# assurance, not the entrypoint. Where the check happened is not a property of the
# person's identity and does not belong in their credential.
#
# A constant rather than a setting, deliberately. A deployment that could edit this
# could make the credential claim an assurance level nobody established, which is
# the exact failure `verified_by` exists to prevent (ds `D-53`).
#
# Since 2026-09-14 the approval door says *how* the REC checked, as a suffix taken
# from the verification its operator recorded before approving —
# `submission-review:offline` or `submission-review:uploaded-document` (see
# `verification_method_for`). The value still comes from what somebody recorded,
# never from configuration. The participant wizard door has no such record and
# still sends the bare value.
VERIFICATION_METHOD = CREDENTIAL_METHOD_PREFIX


def verification_method_for(submission: Any) -> str:
    """The credential's `verificationMethod` for an approved submission.

    The recorded verification's method, prefixed. The bare `submission-review`
    only for a submission approved before verifications were recorded — a retry
    of its enablement must not claim a method nobody wrote down.
    """
    current = getattr(submission, "verification", None)
    if current is None:
        return VERIFICATION_METHOD
    return current.verification_method.credential_value


@dataclass(frozen=True, slots=True)
class RegistryAccess:
    """One registry, one token, for a whole provisioning flow.

    Resolve, check and issue are three calls against the same instance, and
    before this they each fetched their own token and re-derived the same base
    URL. One handle means one token fetch per flow and makes it impossible for
    two calls in one flow to address different registries — which after
    `D-49`/`DID-11` is a mistake the topology now permits.

    Every route this service calls is served by the **anchor**: ds's
    `identity_registry/roles.py` marks `/credentials/check`, `/admin/*` and
    `/memberships` `ANCHOR_ONLY`, and `/users/*` `BOTH`. The holder-side routes
    (`/credentials/{did}/presentations/query`, `/sts`) are not called from here.
    So one URL is correct, and this type is where that stops being an accident.
    """

    base_url: str
    headers: dict[str, str]


async def registry_access() -> RegistryAccess:
    """The anchor identity-registry, authenticated as this service."""
    if not settings.identity_registry_url:
        raise ValueError("IDENTITY_REGISTRY_URL is required when dataspace VC is enabled")
    return RegistryAccess(
        base_url=settings.identity_registry_url.rstrip("/"),
        headers=await _auth_headers(),
    )


class SubjectIdentifierConflictError(ValueError):
    """`/users/resolve` answered 409: the identifier matches somebody else's row.

    ds quarantines rather than reconciles, because "account re-created" and
    "address recycled to a different human" are indistinguishable from there —
    so this is an operator's decision and a member cannot act on it. Raised as
    its own type precisely so a member-facing caller can say *"we cannot confirm
    your dataspace identity; your REC manager has been notified"* and offer no
    retry, instead of the "registry unavailable, try later" that every non-200
    used to read as and that a member could retry forever without the state
    changing.
    """


@dataclass(frozen=True, slots=True)
class ResolvedSubject:
    """What the registry knows about a person before anything is issued.

    ``did`` is ``None`` when no mapping exists yet — not an error but the ordinary
    first-time answer, carrying a freshly minted ``subject_id`` (see
    :func:`new_subject_id`) and nothing else.
    """

    subject_id: str
    did: str | None = None


@dataclass(frozen=True, slots=True)
class SubjectFacts:
    """Who is becoming a dataspace subject, and on whose authority.

    The argument that replaced ``Submission`` on this path. Provisioning is a
    **function, not a stage**: a preregistered member — screened offline, meter
    installed on signature — has no submission and never will, and keying it on
    one made the funnel the only door. Whoever holds an authenticated member with
    no dataspace identity fills these in.
    """

    subject_id: str
    role: str
    email: str | None = None
    keycloak_user_id: str | None = None
    keycloak_realm: str | None = None
    keycloak_username: str | None = None
    verified_by: str | None = None
    verification_method: str | None = None
    allowed_actions: tuple[str, ...] = ()
    ttl_days: int | None = None


@dataclass(frozen=True, slots=True)
class SubjectIdentity:
    """The dataspace identity a person holds once this function returns."""

    did: str
    credential_id: str
    issued_at: datetime


async def resolve_subject(
    access: RegistryAccess,
    *,
    email: str | None = None,
    keycloak_realm: str | None = None,
    keycloak_user_id: str | None = None,
    username: str | None = None,
    recorded: str | None = None,
) -> ResolvedSubject:
    """Ask the registry who this person is, minting an id only if nobody knows.

    **In order, and the first answer wins:**

    1. the registry's mapping — ``derive=false``, so a 404 means *no mapping*
       and is not an error. A person who already has a DID keeps it; minting
       beside it would split their consent records and provenance in two;
    2. ``recorded``, an id this service already minted for them and wrote down
       (the funnel's ``Submission.dataspace_subject_id``). It covers the case
       the registry cannot: a DID issued, and no mapping written, because the
       step failed after issuance or ran with no Keycloak user to map;
    3. a new random id, :func:`new_subject_id`.

    The registry is **never asked to derive**. It would answer an HMAC of the
    email, and the maintainer decided on 2026-09-21 that the ids this setup
    mints are random UUIDs. A failure other than the 404 is fatal — the
    credential issuance that follows requires the same service, so swallowing
    the error would only delay it.

    Returns the DID too when a mapping exists. The response also carries the
    person's ``vc_jws``; it is deliberately **not** read here. This function's job
    is identification, and a credential that is never lifted out of the response
    cannot leak from a caller that did not need it — see
    :func:`resolve_subject_credential` for the path that does need it.
    """
    params: dict[str, str] = {"derive": "false"}
    if email:
        params["email"] = email
    if username:
        params["username"] = username
    if keycloak_realm and keycloak_user_id:
        params["realm"] = keycloak_realm
        params["user_id"] = keycloak_user_id

    body = await _resolve_raw_with_params(access, params)
    return _resolved_from(body, recorded=recorded)


def _resolved_from(body: dict[str, Any] | None, *, recorded: str | None = None) -> ResolvedSubject:
    if body is not None:
        return ResolvedSubject(subject_id=body["subject_id"], did=body.get("did") or None)
    return ResolvedSubject(subject_id=(recorded or "").strip() or new_subject_id())


async def _resolve_raw(access: RegistryAccess, *, email: str) -> dict[str, Any] | None:
    """:func:`resolve_subject`'s call, returning the body rather than a summary.

    Separate because :func:`resolve_subject_credential` needs the ``credentials``
    list, and :func:`resolve_subject` deliberately does not read it — an
    identification function that lifted a live credential out of the response
    would be exactly what moving this away from the BFF was meant to stop.

    ``None`` when the registry holds no mapping for this person.
    """
    return await _resolve_raw_with_params(access, {"email": email, "derive": "false"})


async def _resolve_raw_with_params(
    access: RegistryAccess, params: dict[str, str]
) -> dict[str, Any] | None:
    """The ``/users/resolve`` body, or ``None`` for the registry's 404.

    A 404 is *no mapping for this user*: the answer ``derive=false`` gives for
    somebody the registry has never been told about, and the ordinary first-time
    case rather than a failure.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{access.base_url}/users/resolve", params=params, headers=access.headers
        )

    if resp.status_code == 409:
        # Loud, and with the identifiers, because nobody else will look. The
        # member gets a terminal explanation and no retry; this line is the only
        # thing that tells an operator there is something to decide.
        logger.error(
            "Identity conflict resolving subject (%s): %s",
            ", ".join(f"{k}={v!r}" for k, v in params.items() if k != "derive"),
            resp.text,
        )
        raise SubjectIdentifierConflictError(
            "The identity registry holds a mapping for this identifier under a "
            "different Keycloak user. Only an operator can resolve it; retrying "
            "will not."
        )

    if resp.status_code == 404:
        return None

    if resp.status_code == 200:
        body = resp.json()
        if body.get("subject_id"):
            return body

    raise ValueError(f"Subject resolution failed: identity registry returned {resp.status_code}")


@dataclass(frozen=True, slots=True)
class SubjectCredential:
    """What a member needs in order to act on their own consent.

    The connector authenticates a data subject **by verifiable credential**
    (`X-Subject-Id` + `X-User-VC`), never by a service token — a service account
    that could grant consent on somebody's behalf would defeat the point of
    recording it. So the only thing done for the member is *resolving* which
    credential is theirs; every decision is then presented with it.

    **Never put one of these in a response, and never cache one across
    requests.** It authenticates as that person.
    """

    subject_id: str
    vc_jws: str
    #: What a member can be shown, and quote to a REC manager. Read from the same
    #: registry entry as ``vc_jws`` because they describe *that* credential — the
    #: one being presented — and a role taken from a different entry would
    #: describe a capacity the member is not acting in.
    role: str | None = None
    issued_at: str | None = None
    expires_at: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Subject-Id": self.subject_id, "X-User-VC": self.vc_jws}

    def __repr__(self) -> str:
        """Never render the credential.

        A dataclass repr would print `vc_jws` in full, and this object reaches
        exception messages, log records and test failure output. One of those
        eventually gets shipped somewhere.
        """
        return f"SubjectCredential(subject_id={self.subject_id!r}, vc_jws=<redacted>)"


async def resolve_subject_and_credential(
    access: RegistryAccess,
    *,
    email: str,
) -> tuple[ResolvedSubject, SubjectCredential | None]:
    """Who this person is, and the credential they can act with, in one call.

    Both answers come out of the same ``/users/resolve`` body, and a caller that
    may have to provision needs both: the ``subject_id`` to issue against, and
    the credential to know whether to. Asking twice was a real cost — the second
    read told nobody anything the first had not already said.

    The credential is ``None`` when they hold none, which is an ordinary answer:
    a participant enabled before the dataspace existed has no credential, and
    neither does a preregistered member who has not been provisioned yet.

    **Selected by role, not by recency.** One human legitimately holds several
    credentials — a data subject about their own consumption, a consumer user
    acting for somebody else — and the registry returns them all. The singular
    ``role`` / ``vc_jws`` fields are the most recently issued one, which for such
    a person is the wrong credential; presenting it to the consent API would
    authenticate them in a capacity they are not acting in. The singular fields
    are a fallback, and only when they name the right role.

    Raises :class:`SubjectIdentifierConflictError` on a 409, which a
    member-facing caller must not present as a retryable failure.
    """
    body = await _resolve_raw(access, email=email)
    resolved = _resolved_from(body)
    if body is None:
        return resolved, None

    subject_id = body.get("did") or body.get("subject_did")
    if not subject_id:
        return resolved, None

    for credential in body.get("credentials") or []:
        if credential.get("role") == settings.dataspace_user_role and credential.get("vc_jws"):
            return resolved, _as_subject_credential(subject_id, credential)

    if body.get("role") == settings.dataspace_user_role and body.get("vc_jws"):
        return resolved, _as_subject_credential(subject_id, body)

    return resolved, None


async def resolve_subject_credential(
    access: RegistryAccess,
    *,
    email: str,
) -> SubjectCredential | None:
    """The member's own credential, for a caller that needs nothing else."""
    _, credential = await resolve_subject_and_credential(access, email=email)
    return credential


def _as_subject_credential(subject_id: str, entry: dict[str, Any]) -> SubjectCredential:
    def _text(value: Any) -> str | None:
        return str(value) if value else None

    return SubjectCredential(
        subject_id=subject_id,
        vc_jws=entry["vc_jws"],
        role=_text(entry.get("role")),
        issued_at=_text(entry.get("issued_at")),
        expires_at=_text(entry.get("expires_at")),
    )


async def provision_subject(
    access: RegistryAccess,
    facts: SubjectFacts,
    binding: template_service.DataspaceBinding,
) -> SubjectIdentity:
    """Mint a person's dataspace identity, and tell Keycloak who they are.

    **The whole of provisioning, taking facts rather than a database row.** Both
    doors converge here and differ only in who established that the person may
    become a subject — a REC manager approving a submission, or the
    preregistration the REC did offline. The credential records the assurance
    either way; see :data:`VERIFICATION_METHOD` for why it does not record which
    door.

    **This function always calls, and the registry decides whether that mints.**
    A repeat call for somebody who already holds an active credential *in the
    same role* returns the one they hold — same ``credentialId``, no second
    status-list index — and re-delivers it to their custodian. A different role
    mints, because roles are additive: one person is a data subject about their
    own consumption and may be a consumer user acting for somebody else. The
    subject DID is settled by the first call and reused by every later one.

    **That was not always true, and both doors were built when it was not**: a
    repeat call used to mint a fresh credential and spend a status-list index,
    which is never recovered. Both guards are still right, for reasons that have
    moved:

    * The **funnel** does not ask. A manager has just approved this submission,
      and the row needs a ``dataspace_vc_id`` of its own to stay revocable — which
      it gets whether the registry minted or matched, since the response names the
      credential either way.
    * The **wizard** asks by calling :func:`resolve_subject_credential` first and
      provisioning only when it answers ``None``. Keep it. It is the stronger
      question — it proves the credential can be **read back**, which is what the
      member's own consent calls then present — and it is a call that path already
      makes. The registry's per-role match is a floor, not a substitute: it says a
      credential exists, not that this service can resolve one.

    Order matters and matches :func:`revoke_user_identity` in reverse: credential,
    then membership, then the Keycloak mapping — the membership has a foreign key
    to the DID. A Keycloak sync that fails after its retries rolls both back.
    """
    base_url, headers = access.base_url, access.headers

    body: dict[str, Any] = {"subject_id": facts.subject_id, "role": facts.role}
    if facts.ttl_days is not None:
        body["ttl_days"] = facts.ttl_days
    if binding.linked_participant_did:
        body["linked_participant_did"] = binding.linked_participant_did
    if facts.allowed_actions:
        body["allowed_actions"] = list(facts.allowed_actions)
    if facts.verified_by:
        body["verified_by"] = facts.verified_by
    if facts.verification_method:
        body["verification_method"] = facts.verification_method

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base_url}/admin/credentials/data-subject",
            json=body,
            headers=headers,
        )
        if resp.status_code >= 400:
            raise ValueError(f"Credential issuance failed ({resp.status_code}): {resp.text}")

        evidence = resp.json()

    did: str | None = evidence.get("subjectDid")
    cred_id: str | None = evidence.get("credentialId")
    if not did or not cred_id:
        raise ValueError("Identity-registry response is missing subjectDid or credentialId")

    org_alias = binding.organization
    if org_alias:
        await _register_membership(base_url, headers, did, org_alias)

    if facts.keycloak_user_id and facts.keycloak_realm:
        await _sync_keycloak(
            base_url,
            headers,
            did=did,
            keycloak_user_id=facts.keycloak_user_id,
            keycloak_realm=facts.keycloak_realm,
            email=facts.email,
            username=facts.keycloak_username,
            credential_id=cred_id,
            organization_alias=org_alias,
        )

    return SubjectIdentity(
        did=did,
        credential_id=cred_id,
        issued_at=_parse_generated_at(evidence.get("generatedAt")),
    )


async def provision_user_identity(
    submission: Submission,
    *,
    keycloak_user_id: str | None = None,
    keycloak_realm: str | None = None,
    keycloak_username: str | None = None,
    provision_shares: bool = True,
) -> None:
    """The funnel's door into :func:`provision_subject`.

    Reads the facts off an approved submission, provisions, and writes the
    resulting identity back onto the row. **The authority here is a submission a
    REC manager approved**; the wizard's is the REC's offline preregistration,
    and both record the same assurance.

    ``keycloak_username`` is what provisioning read back from Keycloak. It is the
    same value that becomes ``Member.user_id`` in the REC registry, and passing
    it here is what lets the data plane join the two — see :func:`_sync_keycloak`.
    Optional, because a retry of this step alone has no provisioning result to
    read it from; the registry then falls back to the email, which is right for
    every user this service created and wrong only for one it adopted.
    """
    if not settings.dataspace_enabled:
        return
    if submission.dataspace_vc_id:
        return

    if not settings.identity_registry_url:
        raise ValueError("IDENTITY_REGISTRY_URL is required when dataspace VC is enabled")

    # The binding comes from the REC's manifest, so the manifest cache has to be
    # authoritative before it is read. Approval runs outside the API request path
    # that normally refreshes it, and a stale cache here would silently resolve to
    # "this community is not in the dataspace".
    await template_service.ensure_fresh()
    binding = template_service.dataspace_binding(submission.rec_slug)

    # Two gates, and both must be open. `DATASPACE_ENABLED` says this deployment
    # talks to a dataspace at all; the manifest block says *this community* is in
    # one. A REC without a block gets no credential — issuing one would hand
    # somebody an identity belonging to no organisation, which the consent
    # endpoints refuse to act on anyway.
    if not binding.enabled:
        logger.debug(
            "REC %r declares no dataspace binding; skipping identity provisioning",
            submission.rec_slug,
        )
        return

    access = await registry_access()

    if not submission.email:
        raise ValueError("Cannot resolve a subject id: submission has no email")
    resolved = await resolve_subject(
        access, email=submission.email, recorded=submission.dataspace_subject_id
    )
    # Written **before** issuing, because issuance creates the DID and a random
    # id is not re-derivable. If a later part of this step fails — the Keycloak
    # sync that writes the registry's mapping, say — enablement commits the
    # failed step together with this row, and the retry finds the id here
    # instead of minting a second DID for the same person.
    submission.dataspace_subject_id = resolved.subject_id

    identity = await provision_subject(
        access,
        SubjectFacts(
            subject_id=resolved.subject_id,
            role=settings.dataspace_user_role,
            email=submission.email,
            keycloak_user_id=keycloak_user_id,
            keycloak_realm=keycloak_realm,
            keycloak_username=keycloak_username,
            # The REC established this person's identity, by reviewing the
            # submission its manager approved. `organization_did` is the only
            # authority this service can name without inventing one.
            verified_by=binding.organization_did or None,
            verification_method=verification_method_for(submission),
            allowed_actions=tuple(
                a.strip() for a in settings.dataspace_allowed_actions.split(",") if a.strip()
            ),
            ttl_days=settings.dataspace_vc_ttl_days,
        ),
        binding,
    )

    submission.dataspace_did = identity.did
    submission.dataspace_vc_id = identity.credential_id
    submission.dataspace_vc_issued_at = identity.issued_at

    # Standing data-sharing consent, if the person opted in. Deliberately the
    # LAST step and deliberately non-fatal: a failed share is recoverable, and
    # tearing down a valid identity because a consent row didn't write is the
    # wrong trade (§3.5). The rollback above does not extend here.
    if provision_shares and settings.ds_connector_url and submission.data_sharing_consent:
        try:
            await provision_user_shares(submission)
        except Exception:
            logger.exception(
                "Share provisioning failed for %s; identity kept, retry from admin",
                submission.ref,
            )
            submission.share_provisioned = False


def _evidence_problems(submission: Submission) -> list[str]:
    """Why this submission's consent evidence would be refused, if it would be.

    Mirrors the connector's own rules so the refusal happens here, with a message
    naming this submission, rather than as a 422 on a path that is deliberately
    non-fatal and therefore easy to miss.
    """
    problems: list[str] = []

    for label, value in (
        ("consent text version", submission.data_sharing_consent_text_version),
        ("rendered text hash", submission.data_sharing_consent_text_sha256),
    ):
        if not (value or "").strip():
            problems.append(f"no {label} recorded")

    # The connector rejects an '@' in these fields. It catches the commonest leak
    # — an email used as a reference — not every case; codes-and-hashes-only
    # remains this service's obligation, and `submission_ref` is the only
    # identifier that leaves onboarding at all.
    for label, value in (
        ("submission ref", submission.ref),
        ("rec slug", submission.rec_slug),
    ):
        if "@" in (value or ""):
            problems.append(f"{label} looks like an email address")

    return problems


@dataclass(frozen=True, slots=True)
class ConsentRoute:
    """Where one offer's decision is recorded, and on whose authority.

    **A consent belongs at the connector that serves the data**, which is not
    always the community's own. A grid operator holds its members' meter
    readings; a decision to release them recorded at the community's connector
    enforces nothing, because the data plane that answers for those rows never
    reads it. The community is then the *collector* — the member's relationship
    is with it — and the holder accepts its registrations because it has recorded
    the community as an accepted consent collector.

    ``collector`` is the community's organisation alias, and it is what the token
    is fetched for: the registration is an act of that organisation, whichever
    connector it lands at.
    """

    offer_id: str
    connector_url: str
    collector: str
    #: The participant whose connector this is, when it is not the community's
    #: own. ``None`` is the ordinary case and means *here*.
    holder: str | None = None

    @property
    def is_holder(self) -> bool:
        """Whether this registration crosses into another participant's connector."""
        return self.holder is not None

    @property
    def where(self) -> str:
        return f"{self.holder}'s connector" if self.holder else "this community's connector"


@dataclass(frozen=True, slots=True)
class ShareRegistration:
    """What one connector answered about one offer.

    ``missing_prerequisites`` is ds's: the offers this one is admitted only
    together with (`requires_offers`) that the subject has not granted *there*.
    A registration with a non-empty list is recorded and admits nobody yet, which
    is worth carrying back rather than discovering as an empty row filter — the
    member granted research but not release, and only the holder can say so.
    """

    offer_id: str
    ok: bool
    detail: str = ""
    missing_prerequisites: tuple[str, ...] = ()


def consent_routes(
    binding: template_service.DataspaceBinding,
    own_connector_url: str,
    offer_ids: list[str],
) -> list[ConsentRoute]:
    """One route per connector holding data each offer reaches, from the REC's manifest.

    Usually one per offer. An offer whose data sits in several places — the
    grid operator's readings *and* the community's own meter datasets — has one
    route to each (ADR-0007), the community's own first when it is one of them.

    Configuration, never inference. An offer's ``recipients.recipient`` says who
    the data goes *to*, which since ds's rename is emphatically not who holds it:
    a release offer's recipient is the community, and the rows sit at the grid
    operator. Routing by the recipient would send every release decision to the
    connector that does not serve them.
    """
    own = own_connector_url.rstrip("/")
    routes: list[ConsentRoute] = []
    for offer_id in offer_ids:
        if binding.recorded_here(offer_id):
            routes.append(
                ConsentRoute(offer_id=offer_id, connector_url=own, collector=binding.organization)
            )
        for connector in binding.connectors_for(offer_id):
            routes.append(
                ConsentRoute(
                    offer_id=offer_id,
                    connector_url=connector.url.rstrip("/"),
                    collector=binding.organization,
                    holder=connector.holder,
                )
            )
    return routes


#: The authorities whose decision is **the member's**. ``subject`` is the member,
#: or their community relaying them; ``operator`` is ds's evidenced override,
#: taken at the member's own request (``SubjectWithdrawalOverride``). A
#: ``collector`` withdrawal (a membership that ended) and a ``service`` one (the
#: retired plain-service path) were decided *about* the member, not by them.
_MEMBERS_OWN = frozenset({"subject", "operator"})

#: What ds accepts back as evidence (``AdminShareLegalBasis``, ``extra="forbid"``).
#: A stored row carries more — the connector's own ``offer_id``, ``recipient``,
#: ``user_visible_hash`` — and sending those is refused.
_EVIDENCE_FIELDS = (
    "source",
    "rec_slug",
    "basis_iri",
    "consent_text_version",
    "locale",
    "rendered_text_sha256",
    "accepted_at",
    "submission_ref",
)
_EVIDENCE_REQUIRED = ("source", "consent_text_version", "rendered_text_sha256")

#: Older than any decision: a row carrying no time at all (ds always sends one).
_NEVER = datetime.min.replace(tzinfo=UTC)

#: How many times the retry reads, writes and reads again before it gives up on
#: decisions that keep changing under it. Two writing passes and a verifying read:
#: the second pass is what undoes a grant that landed after the member withdrew.
_CONVERGE_PASSES = 3


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _decision_time(row: dict[str, Any]) -> datetime:
    """When the decision a connector row records was **taken**.

    ds's own rule (``consent_service.decision_time``): ``revoked_at``, then
    ``decided_at``, then ``requested_at`` — because withdrawing a standing grant
    *mutates* that row, stamps ``revoked_at`` and leaves ``decided_at`` at the
    grant's time. Reading ``decided_at`` alone would date every withdrawal to
    the grant it withdrew.

    One addition, for a grant relayed with evidence: the evidence's
    ``accepted_at`` says when the member accepted, which is earlier than the
    moment the row was written whenever the write was a relay — the form's
    acceptance, recorded at approval, or a decision the retry carried to a
    second connector. Dating a relay by its write would let it outrank a
    withdrawal the member made after the decision it relays, which is the race
    this ranking exists to close. Never later than the row itself.

    **Not for an ``operator`` row.** ds's evidenced override is itself the act —
    taken when it was written, at the member's request, usually to lift a
    withdrawal — and the evidence it carries may be the original consent's.
    Dated by that, the override would rank before the withdrawal it lifted.
    """
    recorded = _NEVER
    for key in ("revoked_at", "decided_at", "requested_at"):
        moment = _instant(row.get(key))
        if moment is not None:
            recorded = moment
            break
    if row.get("status") == "granted" and row.get("decided_by") != "operator":
        accepted = _instant((row.get("legal_basis") or {}).get("accepted_at"))
        if accepted is not None and (recorded is _NEVER or accepted < recorded):
            return accepted
    return recorded


@dataclass(frozen=True, slots=True)
class _Decision:
    """One decision the member took, where it is recorded, and when."""

    granted: bool
    at: datetime
    where: str
    #: A grant's stored evidence, relayed with it to a connector that lacks it.
    evidence: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _Held:
    """What one connector records for one of the member's offers."""

    #: Whether any of its rows stands (one row per dataset; the offer counts as
    #: granted if any does — the rule the member's page reads with).
    granted: bool
    #: The member's own newest decision there, if it records one.
    member: _Decision | None
    #: The data keys of each standing grant whose keys the connector returned,
    #: one set per row. ds returns a row's keys only to the organisation that
    #: registered them (``_keys_for``, ADR-0022), so these are the keys this
    #: community sent; a grant another party wrote shows none and is not here.
    keys: tuple[frozenset[str], ...] = ()


async def _subject_rows(
    client: httpx.AsyncClient, route: ConsentRoute, *, subject_id: str
) -> list[dict[str, Any]]:
    """Every row ``route``'s connector lists for this member, as ds answers them.

    ``GET /consent/admin/subject-shares`` as the community's own organisation
    client — which ds admits at the community's connector and at a holder that
    accepted it as a collector, for its own members only. It lists the latest
    decision per dataset, with ``decided_by``, ``collector`` and the times.

    Raises when the connector cannot say: what it records is then unknown.
    """
    try:
        headers = await organisation_auth_headers(route.collector)
    except Exception as exc:
        # Same reasoning as `register_share`: the reason names this deployment's
        # own settings and belongs in the log, not in a step row a REC manager
        # reads.
        logger.exception(
            "Cannot authenticate as %s to read consent at %s",
            route.collector or "<no organisation>",
            route.where,
        )
        raise RuntimeError(
            "this community's own dataspace client is not configured — see the server log"
        ) from exc
    try:
        resp = await client.get(
            f"{route.connector_url}/consent/admin/subject-shares",
            params={"subject_id": subject_id},
            headers=headers,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(str(exc) or type(exc).__name__) from exc
    if resp.status_code >= 400:
        raise RuntimeError(f"{resp.status_code} {resp.text}")
    body = resp.json()
    rows = body if isinstance(body, list) else (body or {}).get("items", [])
    return [row for row in rows if isinstance(row, dict)]


async def _read_connector(
    client: httpx.AsyncClient, route: ConsentRoute, *, subject_id: str
) -> dict[str, _Held]:
    """What ``route``'s connector records for this member, by offer.

    Raises when the connector cannot say (:func:`_subject_rows`): the caller
    must then not write, because without it the member's newest decision is
    unknown.
    """
    rows = await _subject_rows(client, route, subject_id=subject_id)

    by_offer: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not row.get("offer_id"):
            continue
        if row.get("status") == "pending":
            # A consumer's ask, not a decision.
            continue
        by_offer.setdefault(str(row["offer_id"]), []).append(row)

    held: dict[str, _Held] = {}
    for offer_id, offer_rows in by_offer.items():
        own = [r for r in offer_rows if r.get("decided_by", "subject") in _MEMBERS_OWN]
        member = None
        if own:
            newest = max(own, key=lambda r: (_decision_time(r), r.get("status") != "granted"))
            granted = newest.get("status") == "granted"
            member = _Decision(
                granted=granted,
                at=_decision_time(newest),
                where=route.where,
                evidence=newest.get("legal_basis") if granted else None,
            )
        held[offer_id] = _Held(
            granted=any(r.get("status") == "granted" for r in offer_rows),
            member=member,
            keys=tuple(
                frozenset(str(k) for k in r["keys"])
                for r in offer_rows
                if r.get("status") == "granted"
                and str(r.get("consumer_id") or _ANY_CONSUMER) == _ANY_CONSUMER
                and isinstance(r.get("keys"), list)
                and r["keys"]
            ),
        )
    return held


def _newest(decisions: list[_Decision]) -> _Decision:
    """The member's newest decision. On a tie, the withdrawal (GDPR Art. 7(3))."""
    return max(decisions, key=lambda d: (d.at, not d.granted))


def _relayable(evidence: Any) -> dict[str, Any] | None:
    """A stored legal basis cut down to what ds accepts back, if it proves anything."""
    if not isinstance(evidence, dict):
        return None
    if not all(str(evidence.get(key) or "").strip() for key in _EVIDENCE_REQUIRED):
        return None
    return {key: evidence[key] for key in _EVIDENCE_FIELDS if evidence.get(key) is not None}


def _held_in_several_places(binding: template_service.DataspaceBinding) -> list[str]:
    """The offers the manifest records at more than one connector — the ones that can split."""
    named = dict.fromkeys(
        [*binding.own_offers, *(offer for c in binding.connectors for offer in c.offers)]
    )
    return [
        offer
        for offer in named
        if len(binding.connectors_for(offer)) + int(binding.recorded_here(offer)) > 1
    ]


async def subject_supply_keys(
    rec_slug: str, did: str, *, declared_pod: str | None = None
) -> list[str]:
    """The member's supply points, typed, for a registration at a holder.

    ``["pod:EX000E00000001"]``. The holder's data plane keys its rows by supply
    point and knows nothing about the community's members, so these are what
    turns a consent into rows: ds stores them on the consent row and carries them
    in the row filter beside the principals, and the dataset-api matches them.

    **The registry is the source, and the intake form only when there is no
    registry.** An operator's correction through onboarding (a revision) reaches
    both ``submissions.pod_code`` and the registry; a POD corrected or retired
    directly in the registry never reaches ``submissions.pod_code``, and the
    export learned the same lesson: two records of one fact disagree, and the
    running system is the one that is right. A community with no
    ``REC_REGISTRY_URL`` (or no ``rec_registry`` block) has only the declared
    value, which is better than nothing and is why the fallback exists at all.
    A registry that cannot be read falls back to the declared value too
    (:func:`_supply_keys` says when it did).

    An empty answer is an answer: the member holds nothing the registry knows of,
    and the caller refuses the registration rather than recording a consent that
    can never yield a row.
    """
    keys, _ = await _supply_keys(rec_slug, did, declared_pod=declared_pod)
    return keys


async def _supply_keys(
    rec_slug: str, did: str, *, declared_pod: str | None = None
) -> tuple[list[str], bool]:
    """:func:`subject_supply_keys`, and whether they are the system's own answer.

    The flag is ``False`` only when a registry is configured and could not be
    read, so the keys are the declared value standing in for it. Good enough to
    grant with — the existing behaviour — and not good enough to *replace* keys a
    holder already holds: those may be a correction the declared value predates.
    """
    from celine.onboarding.services import rec_registry

    pods: list[str] | None = None
    authoritative = True
    try:
        found = await rec_registry.supply_points_by_did([did], rec_slug=rec_slug)
    except Exception as exc:  # noqa: BLE001 — reported by the caller as a refusal
        logger.warning("Could not read supply points for %s from the registry: %s", did, exc)
        found = None
        authoritative = False
    else:
        if found is not None:
            pods = found.get(did, [])

    if pods is None:
        # No registry to ask. Not the same as "the registry knows of none", which
        # is `[]` and stands.
        pods = [declared_pod.strip()] if declared_pod and declared_pod.strip() else []

    return [f"pod:{pod}" for pod in dict.fromkeys(pods) if pod], authoritative


async def register_share(
    client: httpx.AsyncClient,
    route: ConsentRoute,
    *,
    subject_id: str,
    enabled: bool,
    decided_by: str,
    legal_basis: dict[str, Any] | None = None,
    keys: list[str] | None = None,
    reason: str | None = None,
) -> ShareRegistration:
    """Register one standing decision at the connector that holds the data.

    **As the community, never as this service.** ds classifies the caller from
    the verified token and refuses a plain service client: one shared service
    account is bound to no participant, so it could write at any connector for
    anybody's members. The token here is the collector's own organisation client,
    which is also what lets it write at a holder that accepted it as a collector.

    ``decided_by`` is not optional and not a detail. ``subject`` relays a decision
    the member took — and a relayed *withdrawal* is then the member's, which
    nothing else can lift; ``collector`` records one the organisation took
    itself, which it may lift again. Sending the wrong one is a decision
    attributed to the wrong person.

    ``keys`` travel with a grant only. ds refuses them on a withdrawal — a
    withdrawal drops the keys it had — and they are personal data, so they are
    sent only where they are needed: a holder's data plane has no other way to
    find this member's rows, and the community's own connector resolves its
    members without them.

    ``reason`` is why the community withdrew, and it travels **only** with the
    community's own withdrawal (``enabled=False``, ``decided_by="collector"``) —
    ds records it as the row's ``revocation_reason`` and refuses it anywhere
    else (its ADR-0019): a relayed withdrawal is the member's, so the
    community's words would be filed as the cause of a decision it did not take,
    and a grant has no cause. Refused here before anything is sent, and passed
    through :func:`withdrawal_reason` by the one caller that sends it. There is
    no ``message``: ds never accepted one on this route.
    """
    if reason is not None and (enabled or decided_by != "collector"):
        raise ValueError(
            "a reason is recorded only on the community's own withdrawal "
            "(enabled=False, decided_by='collector')"
        )
    body: dict[str, Any] = {
        "subject_id": subject_id,
        "offer_id": route.offer_id,
        "enabled": enabled,
        "decided_by": decided_by,
    }
    if legal_basis is not None:
        body["legal_basis"] = legal_basis
    if reason:
        body["reason"] = reason
    if enabled and keys:
        body["keys"] = keys

    try:
        headers = await organisation_auth_headers(route.collector)
    except Exception:  # noqa: BLE001 — reported per offer, never raised from here
        # Logged in full and summarised in the answer. The reason names this
        # deployment's own settings, and the answer reaches an operator's console
        # as a 422 body: the person who can fix it is reading the log, and the
        # REC manager retrying a share is not (`services.errors`).
        logger.exception(
            "Cannot authenticate as %s to register consent at %s",
            route.collector or "<no organisation>",
            route.where,
        )
        return ShareRegistration(
            route.offer_id,
            ok=False,
            detail=(
                "this community's own dataspace client is not configured, so the "
                "decision cannot be registered — see the server log"
            ),
        )

    try:
        resp = await client.post(
            f"{route.connector_url}/consent/admin/shares", json=body, headers=headers
        )
    except httpx.HTTPError as exc:
        return ShareRegistration(route.offer_id, ok=False, detail=f"{route.where}: {exc}")

    # Nothing to withdraw is the state a withdrawal wants.
    if resp.status_code == 404 and not enabled:
        return ShareRegistration(route.offer_id, ok=True, detail="nothing to withdraw")

    if resp.status_code >= 400:
        logger.error(
            "Consent registration for offer %s at %s failed (%s): %s",
            route.offer_id,
            route.where,
            resp.status_code,
            resp.text,
        )
        return ShareRegistration(
            route.offer_id,
            ok=False,
            detail=f"{route.where}: {resp.status_code} {resp.text}",
        )

    missing: tuple[str, ...] = ()
    try:
        rows = resp.json()
    except ValueError:
        rows = []
    if isinstance(rows, list):
        missing = tuple(
            dict.fromkeys(
                str(offer)
                for row in rows
                if isinstance(row, dict)
                for offer in (row.get("missing_prerequisites") or [])
            )
        )
    if missing:
        # Recorded, and admitting nobody yet. Not a failure — the decision is
        # exactly what the member made — but silence here is how "I consented and
        # nothing happened" becomes unexplainable.
        logger.info(
            "Offer %s is recorded at %s and waits for %s",
            route.offer_id,
            route.where,
            ", ".join(missing),
        )
    return ShareRegistration(route.offer_id, ok=True, missing_prerequisites=missing)


async def relay_member_decision(
    rec_slug: str,
    *,
    subject_id: str,
    offer_id: str,
    enabled: bool,
    legal_basis: dict[str, Any] | None = None,
) -> list[ShareRegistration]:
    """Carry a decision the member just took to every other participant holding the data.

    For the part of an offer the community does not hold. The member cannot act
    there themselves — their credential is linked to their own community's
    participant and the holder refuses it — so their community relays it, as the
    collector, and says the decision is **theirs** (``decided_by="subject"``). A
    withdrawal relayed this way is the member's own, which is the whole point:
    nothing the community or a service does afterwards can lift it.

    One registration per holder of the offer, every one attempted whatever the
    others answer: a withdrawal that reaches one holder of two is a leak, not a
    lag. Their supply points go with a grant, because a holder's data plane has
    no other way to find their rows. Returns nothing for an offer no other
    participant holds.
    """
    binding = template_service.dataspace_binding(rec_slug)
    routes = [
        r for r in consent_routes(binding, settings.ds_connector_url, [offer_id]) if r.is_holder
    ]
    if not routes:
        return []

    keys: list[str] | None = None
    if enabled:
        keys = await subject_supply_keys(rec_slug, subject_id)
        if not keys:
            return [
                ShareRegistration(
                    offer_id,
                    ok=False,
                    detail=(
                        "no supply point is recorded for this member, so "
                        f"{route.where} would have nothing to release"
                    ),
                )
                for route in routes
            ]

    async with httpx.AsyncClient(timeout=30) as client:
        return [
            await register_share(
                client,
                route,
                subject_id=subject_id,
                enabled=enabled,
                decided_by="subject",
                legal_basis=legal_basis,
                keys=keys,
            )
            for route in routes
        ]


async def subject_shares_at_holders(rec_slug: str, *, subject_id: str) -> list[dict[str, Any]]:
    """What every other participant's connector recorded for this member.

    The read-back a collector needs: a member's decisions do not all live in one
    place any more, and the one that matters most — the release — lives where the
    member cannot read it. ``/consent/my/*`` is theirs and refuses an
    organisation token by design, so this is ds's narrow exception: per subject,
    limited to the caller's own members, and never a roster.

    Raises rather than returning a partial list. A holder that cannot be reached
    makes a granted decision look withdrawn, which is the direction that invites
    somebody to grant again what they already granted — and hides a withdrawal
    that has not taken effect.
    """
    binding = template_service.dataspace_binding(rec_slug)
    decisions: list[dict[str, Any]] = []
    for connector in binding.connectors:
        headers = await organisation_auth_headers(binding.organization)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{connector.url}/consent/admin/subject-shares",
                    params={"subject_id": subject_id},
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"{connector.holder}'s connector could not be reached, so what it "
                f"recorded for this member is unknown: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise RuntimeError(
                f"{connector.holder}'s connector answered {resp.status_code} for "
                "this member's decisions"
            )
        body = resp.json()
        rows = body if isinstance(body, list) else body.get("items", [])
        for row in rows:
            if isinstance(row, dict):
                # Where it was recorded, for a reader that now sees two sources
                # in one list. Never the keys: they are on the row and this is
                # rendered to the member's own page.
                decisions.append(
                    {**{k: v for k, v in row.items() if k != "keys"}, "holder": connector.holder}
                )
    return decisions


async def provision_user_shares(
    submission: Submission,
    *,
    raise_on_error: bool = False,
    report: list[str] | None = None,
    force_key_refresh: bool = False,
) -> bool:
    """Bring every connector holding the member's offers to the member's newest decision.

    Called at the end of :func:`provision_user_identity`, by approval's step 4 and
    by the operator's retry of it.  Names an ``offer_id`` per recorded offer —
    never a dataset — so the connector expands each into the datasets the offer
    describes and the onboarding config can never drift from what the person read.

    **One acceptance, several connectors.** The member ticked the boxes once, in
    the form; each accepted offer is then recorded at the connector that holds
    the data it reaches (:func:`consent_routes`), as the community's own
    organisation client and with the form's evidence. Offers whose data is this
    community's go to its own connector, as before; a release offer goes to the
    grid operator's, where the rows are.

    **Whose decision it is: the member's** (``decided_by="subject"``). This
    relays a decision somebody took on a form or on their sharing page, not one
    the community made for them — which matters most for a withdrawal: relayed
    as theirs, nothing but the member can lift it.

    **The newest decision wins, in either direction** (the maintainer,
    2026-09-19). Every connector holding an offer is read first; the member's
    newest decision on it — the form's acceptance, or a grant or withdrawal any
    of those connectors records as theirs (:func:`_decision_time`) — is applied
    at every connector that disagrees, and nothing is written where they agree.
    So a retry writes the missing half of a partial grant, carries a withdrawal
    that landed at one connector to the others, and never lifts a withdrawal
    made after the decision it would relay. A grant carries its decision's time
    (``accepted_at``); a collector's or a service's withdrawal is not the
    member's decision and never outranks one. Then everything is read again, and
    a decision that changed meanwhile is applied too — which is what undoes a
    grant that landed just after the member withdrew.

    **The member's own record ranks with the connectors** (:mod:`sharing_intent`,
    the maintainer, 2026-09-19): what they last pressed on their page, per
    offer, dated when they pressed. It is what a connector may not keep — a
    withdrawal over a standing one is not stamped, and a relayed grant can
    overwrite the one connector a withdrawal reached — and it is re-read with
    the connectors on every pass.

    **A connector, or the member's record, that cannot be read fails the whole
    run and nothing is written**: the newest decision is not known without it.

    Beyond the offers the form accepted, every offer the manifest records in
    more than one place, and every offer the member decided on their page, is
    examined too: a member may have granted one since, and it can split like any
    other. A member who declined everything on the form is examined the same
    way, with no form decision in the ranking.

    **A standing grant is left alone — unless its keys are stale.** At another
    participant's connector, a grant whose data keys (as the holder returns them to
    the community that registered them) differ, as a set, from the member's supply
    points now (:func:`subject_supply_keys`) is sent again with the current keys:
    a POD changed in the registry, outside onboarding's revisions, reaches the
    holder on the next retry. Equal keys are agreement and nothing is written.
    Keys are compared only against the registry's own answer, never against the
    declared POD standing in for a registry that cannot be read. With
    ``force_key_refresh``, every grant the member holds at another participant is
    sent again on the first pass whatever it carries (:func:`refresh_keys`). The
    holder updates the row's keys in place and records the change (ds ADR-0022).

    ``raise_on_error`` is False on the approval path (a failure must not fail
    approval) and True on explicit retry (the operator wants to see it fail).
    ``report`` collects one line per decision written, for the step row. Returns
    whether every connector ended in agreement with the member's decision.
    """
    if not settings.ds_connector_url:
        return False
    if not submission.dataspace_did:
        logger.warning("Cannot provision shares for %s: no dataspace DID", submission.ref)
        return False

    # A member who declined everything on the form has no form decision, and
    # may still have decided on their page since — so they are examined too,
    # with the form left out of the ranking (the maintainer, 2026-09-19).
    accepted = (
        list(submission.data_sharing_consent_offer_ids or [])
        if submission.data_sharing_consent
        else []
    )
    if submission.data_sharing_consent and not accepted:
        logger.warning("data_sharing_consent set but no offers recorded for %s", submission.ref)
        if raise_on_error:
            raise ValueError("No data-sharing offers recorded for this submission")
        return False

    problems = _evidence_problems(submission) if accepted else []
    if problems:
        # Refuse before posting rather than letting the connector 422. The
        # rejection would be identical on every retry — the evidence cannot be
        # reconstructed after the fact — so a clear local message is the only
        # thing that helps whoever looks at this submission next.
        detail = "; ".join(problems)
        logger.error("Refusing to provision shares for %s: %s", submission.ref, detail)
        if raise_on_error:
            raise ValueError(f"Consent evidence is incomplete: {detail}")
        return False

    # The routing is in the manifest, so the cache has to be authoritative before
    # it is read — this runs from the retry endpoint too, and a stale cache would
    # send a release decision to the community's own connector, where it enforces
    # nothing and still answers 200.
    await template_service.ensure_fresh()
    binding = template_service.dataspace_binding(submission.rec_slug)
    subject_id = submission.dataspace_did
    # The member's own record of what they last pressed, per offer. Read before
    # anything is written for the same reason a connector is: without it the
    # member's newest decision is unknown.
    try:
        intents = await sharing_intent.for_subject(subject_id)
    except Exception as exc:  # noqa: BLE001 — reported, and nothing written
        logger.exception("Cannot read the recorded decisions of %s", submission.ref)
        if raise_on_error:
            raise ValueError(
                "could not read the member's recorded decisions, so their newest "
                "decision is unknown and nothing was written"
            ) from exc
        return False
    offer_ids = list(dict.fromkeys([*accepted, *_held_in_several_places(binding), *intents]))
    routes = consent_routes(binding, settings.ds_connector_url, offer_ids)
    accepted_at = (
        submission.data_sharing_consent_at.isoformat()
        if submission.data_sharing_consent_at
        else None
    )
    legal_basis = {
        "source": "onboarding",
        "rec_slug": submission.rec_slug,
        "consent_text_version": submission.data_sharing_consent_text_version,
        "locale": submission.data_sharing_consent_locale,
        "rendered_text_sha256": submission.data_sharing_consent_text_sha256,
        "accepted_at": accepted_at,
        # The submission ref is the only identifier that leaves onboarding.
        # Never a name, email, CF or POD — the connector DB is not a PII store.
        "submission_ref": submission.ref,
    }
    # The form is a decision too: every offer it accepted, granted, when it was.
    form = _Decision(
        granted=True,
        at=_instant(submission.data_sharing_consent_at) or _NEVER,
        where="the onboarding form",
        evidence=legal_basis,
    )

    # Read once, and only when a grant is about to go to another participant or
    # a standing one there is to be compared with them. They are the member's
    # supply points, so asking the registry per offer would only be a way for two
    # offers to disagree about the same person.
    keys: list[str] | None = None
    keys_known = False

    async def supply_keys() -> list[str]:
        nonlocal keys, keys_known
        if keys is None:
            keys, keys_known = await _supply_keys(
                submission.rec_slug, subject_id, declared_pod=submission.pod_code
            )
        return keys

    async def keys_changed(standing: _Held) -> bool:
        """Whether a standing grant carries other keys than the member holds now.

        Compared as sets, per row. Only rows whose keys the holder returned —
        the ones this community registered — and only against keys the registry
        answered: a declared POD standing in for a registry that cannot be read
        may be the very value a correction replaced, so it never replaces keys.
        """
        if not standing.keys:
            return False
        current = frozenset(await supply_keys())
        if not keys_known:
            logger.warning(
                "Not comparing the keys of %s's standing grants: the registry could not be read",
                submission.ref,
            )
            return False
        return any(held_keys != current for held_keys in standing.keys)

    failures: dict[tuple[str, str], str] = {}
    #: Every decision written, so a read that still disagrees after it can be
    #: told apart from a decision that changed meanwhile.
    written: set[tuple[str, str, bool, datetime]] = set()
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(_CONVERGE_PASSES):
            # **Read every connector before writing to any.**
            held: dict[str, dict[str, _Held]] = {}
            unreadable: list[str] = []
            for url in dict.fromkeys(route.connector_url for route in routes):
                route = next(r for r in routes if r.connector_url == url)
                try:
                    held[url] = await _read_connector(client, route, subject_id=subject_id)
                except Exception as exc:  # noqa: BLE001 — reported, and nothing written
                    unreadable.append(
                        f"could not read what {route.where} records for this member, so "
                        f"their newest decision is unknown and nothing was written: {exc}"
                    )
            if attempt > 0 and not unreadable:
                # Again after writing: a member who pressed while this ran is
                # seen here, and outranks what was just written.
                try:
                    intents = await sharing_intent.for_subject(subject_id)
                except Exception as exc:  # noqa: BLE001 — reported, and nothing written
                    unreadable.append(
                        "could not read the member's recorded decisions, so their newest "
                        f"decision is unknown and nothing more was written: {exc}"
                    )
            if unreadable:
                for line in unreadable:
                    failures[("read", line)] = line
                break

            writes: list[tuple[ConsentRoute, _Decision]] = []
            for offer_id in offer_ids:
                offer_routes = [r for r in routes if r.offer_id == offer_id]
                here = {r.connector_url: held[r.connector_url].get(offer_id) for r in offer_routes}
                decisions = [h.member for h in here.values() if h is not None and h.member]
                if offer_id in accepted:
                    decisions.append(form)
                intent = intents.get(offer_id)
                if intent is not None:
                    decisions.append(
                        _Decision(
                            granted=intent.granted,
                            at=_instant(intent.decided_at) or _NEVER,
                            where="their sharing page",
                            evidence=intent.evidence,
                        )
                    )
                if not decisions:
                    continue
                newest = _newest(decisions)
                for route in offer_routes:
                    there = here[route.connector_url]
                    standing = there is not None and there.granted
                    # A standing grant at a holder is sent again when the keys it
                    # carries are not the member's supply points now (a POD
                    # changed in the registry), or once, on the first pass, when
                    # a key refresh is forced. Only a grant: a withdrawal carries
                    # no keys and is never re-sent. Only at a holder: the
                    # community's own connector is never sent keys.
                    refresh = (
                        there is not None
                        and there.granted
                        and newest.granted
                        and route.is_holder
                        and ((force_key_refresh and attempt == 0) or await keys_changed(there))
                    )
                    if newest.granted == standing and not refresh:
                        continue
                    if (route.connector_url, offer_id, newest.granted, newest.at) in written:
                        # Written once already, answered 2xx, and still not
                        # recorded. Sending it again would get the same answer.
                        failures[(route.connector_url, offer_id)] = (
                            f"{offer_id}: {route.where} accepted the member's decision "
                            "and still does not record it"
                        )
                        continue
                    writes.append((route, newest))

            if not writes:
                break
            if attempt == _CONVERGE_PASSES - 1:
                failures[("converge", "")] = (
                    "the member's decisions changed while this ran and the connectors "
                    "still disagree; retry again"
                )
                break

            if any(r.is_holder and d.granted for r, d in writes):
                await supply_keys()

            refused = False
            for route, decision in writes:
                problem = await _redrive(
                    client,
                    route,
                    decision,
                    subject_id=subject_id,
                    rec_slug=submission.rec_slug,
                    keys=keys,
                )
                if problem is not None:
                    failures[(route.connector_url, route.offer_id)] = f"{route.offer_id}: {problem}"
                    refused = True
                    continue
                failures.pop((route.connector_url, route.offer_id), None)
                written.add((route.connector_url, route.offer_id, decision.granted, decision.at))
                if report is not None:
                    report.append(
                        f"{route.offer_id} {'granted' if decision.granted else 'withdrawn'} "
                        f"at {route.where}, as the member decided at {decision.where}"
                    )
            if refused:
                # Not looped on: a connector refusing will refuse again, and a
                # member's decision is not re-sent until an operator asks.
                break

    ok = not failures
    if accepted:
        # This service's memory of the form's consent reaching the connectors. A
        # member who declined on the form has none to remember.
        submission.share_provisioned = ok
    if failures and raise_on_error:
        raise ValueError("Share provisioning failed: " + "; ".join(failures.values()))
    return ok


async def refresh_keys(submission: Submission, *, report: list[str]) -> bool:
    """Re-send the member's grants at every holder with the supply points read now.

    After a corrected POD reached the registry, the holders still key the
    member's rows by the old one. :func:`provision_user_shares` re-sends a
    standing grant whose keys it can read and compare; this is that same run —
    the same reads, the same newest-decision ranking, the same
    :func:`register_share` — with every standing grant at a holder sent once
    more with the new keys, whether or not its keys could be compared. A grant
    the holder refused because no supply point was recorded is not standing, so
    the same run grants it now that one is.

    ``report`` gets one line per grant written (offer and holder, never a key).
    Raises ``ValueError`` when a holder did not take one, as an operator's retry
    does; the reason names offers and connectors, and may quote a connector's
    answer, so the caller logs it rather than showing it.
    """
    return await provision_user_shares(
        submission, raise_on_error=True, report=report, force_key_refresh=True
    )


async def _redrive(
    client: httpx.AsyncClient,
    route: ConsentRoute,
    decision: _Decision,
    *,
    subject_id: str,
    rec_slug: str,
    keys: list[str] | None,
) -> str | None:
    """Carry one of the member's decisions to a connector that lacks it.

    Returns why it did not land, or ``None``. Always ``decided_by="subject"``:
    it is the member's decision, relayed. A withdrawal relayed as the
    community's (``collector``) would be one the community could lift again on
    its own say — exactly what a member's withdrawal must never be.
    """
    if not decision.granted:
        registration = await register_share(
            client,
            route,
            subject_id=subject_id,
            enabled=False,
            decided_by="subject",
            # No `reason`: ds takes one only with the community's own
            # withdrawal, and this one is the member's (found live, 2026-09-19,
            # when it still sent `message` and got a 422).
        )
        return None if registration.ok else registration.detail

    if route.is_holder and not keys:
        # A release decision with no supply points is a consent that can never
        # yield a row: the holder's data plane finds this member only by the keys
        # sent with it. Refused, and retryable once the registry knows what they
        # hold — silence here would read as a working consent for as long as
        # nobody looked.
        return (
            "no supply point is recorded for this member, so "
            f"{route.where} would have nothing to release"
        )

    evidence = _relayable(decision.evidence)
    if evidence is None:
        # A grant the member made at their own connector as themselves: ds built
        # its evidence from the offer and it carries no rendering to relay. The
        # holder gets what the member's page relays for the same act — this
        # service's rendering of the offer it served (`relayed_evidence`).
        from celine.onboarding.services import member_sharing

        try:
            offer = await template_service.get_sharing_offer(rec_slug, route.offer_id)
        except Exception as exc:  # noqa: BLE001 — reported for this route
            return f"no evidence to relay with the member's grant: {exc}"
        evidence = member_sharing.relayed_evidence(offer, rec_slug)
    if decision.at is not _NEVER:
        # The decision's own time, never the moment of this write: a relay dated
        # by its write would outrank a withdrawal made after the decision.
        evidence = {**evidence, "accepted_at": decision.at.isoformat()}

    registration = await register_share(
        client,
        route,
        subject_id=subject_id,
        enabled=True,
        decided_by="subject",
        legal_basis=evidence,
        keys=keys if route.is_holder else None,
    )
    return None if registration.ok else registration.detail


#: ds's limit on a withdrawal's ``reason`` (``AdminShareRequest``, its ADR-0019).
REASON_MAX_LENGTH = 200

#: Sent in place of a reason that would carry an address: say why, not who.
_GENERIC_REASON = "Membership revoked"

#: ds's wildcard consumer: the cell a standing decision about an offer lives in,
#: and the only one ``POST /consent/admin/shares`` writes.
_ANY_CONSUMER = "*"


def withdrawal_reason(text: str) -> str | None:
    """``text`` made into what ds accepts as a withdrawal's ``reason``, or ``None``.

    ds's rules (``AdminShareRequest.reason``): one line, no control character,
    1–200 characters after trimming, and no ``@`` — it reaches provenance, where
    no personal data may go, and an ``@`` is the obvious way an address gets
    in. So every run of whitespace or control characters becomes one space, the
    text is cut to 200, and anything carrying an ``@`` is replaced by a generic
    cause rather than sent: ds would refuse it, and the withdrawal with it.
    Nothing left is no reason (the field is optional).
    """
    printable = "".join(c if c.isprintable() else " " for c in text)
    one_line = " ".join(printable.split())
    if not one_line:
        return None
    if "@" in one_line:
        return _GENERIC_REASON
    return one_line[:REASON_MAX_LENGTH].rstrip()


async def _community_did(binding: template_service.DataspaceBinding) -> str:
    """The DID ds stamps as ``collector`` on every row this community's client writes.

    The manifest's ``organization_did`` when it states one, otherwise the
    identity registry's answer for the community's alias — the organisation
    token's context, which is what ds records. Raises when neither can say:
    without it a grant this community collected cannot be told from another's.
    """
    if binding.organization_did:
        return binding.organization_did
    check = await check_organization(binding.organization)
    if check.did:
        return check.did
    raise RuntimeError(
        f"the dataspace identifier of {binding.organization or 'this community'} is "
        "unknown (no organization_did in the manifest, and the identity registry "
        "did not answer one), so the grants it collected cannot be told apart"
    )


def _collected_here(row: dict[str, Any], *, community: str, own_connector: bool) -> bool:
    """Whether ``row`` is a standing grant this community collected.

    ``collector`` is the organisation whose token wrote the row's current
    state, and ds stamps it on every write. At a holder only the community's
    own DID is its. At the community's own connector, a row with **no**
    collector is its too: the member decided there with their own credential —
    the sharing page — or the community's operator did at their request, and
    either way the connector is the community's. A row another organisation
    collected is never the community's, wherever it sits.
    """
    if row.get("status") != "granted" or not row.get("offer_id"):
        return False
    if str(row.get("consumer_id") or _ANY_CONSUMER) != _ANY_CONSUMER:
        # A consumer's ask the member approved: its own instrument, not a
        # standing decision, and a cell this route never writes.
        return False
    collector = row.get("collector")
    if collector == community:
        return True
    return own_connector and not collector


async def withdraw_user_shares(
    submission: Submission,
    *,
    reason: str = "",
    raise_on_error: bool = False,
    report: list[str] | None = None,
) -> bool:
    """Withdraw every standing grant this community collected for the member.

    Called when a membership is revoked, **before** the identity it is keyed
    on (:func:`revoke_user_identity`) — ds admits the community's read and
    write for a subject only while they are a member of its organisation.

    **Every grant, wherever it came from** (the maintainer, 2026-09-19). The
    form's accepted offers are only what the member ticked once; they may
    have granted more on their sharing page since, and a member who declined
    the form may have granted there too. So the offer list is not asked at
    all: every connector the manifest names — the community's own and every
    holder — is read (``GET /consent/admin/subject-shares``), and each offer
    with a standing grant the community collected there (:func:`_collected_here`)
    is withdrawn there. Nothing else is written: a refusal the member made
    stays theirs, and a grant another organisation collected is not this
    community's to withdraw — ds would accept the write, and stamp it as ours.

    **This one is the community's decision** (``decided_by="collector"``). Nobody
    withdrew: the REC revoked a membership, and the consent goes with it.
    Recording it as the member's would attribute to them an act they did not
    take — and would lock it, since a subject's withdrawal is theirs alone to
    lift. Its cause travels as ``reason`` (:func:`withdrawal_reason`), which ds
    records on the row and in provenance and returns to no reader.

    **The member's recorded intents are left as they are**
    (:mod:`sharing_intent`). They are the member's decisions and this is not
    one; a re-approval ranks them as it ranks the form, and a collector's
    withdrawal never outranks either.

    Every connector is attempted whatever the others answer, and a connector
    that cannot be read fails the revocation — what stands there is unknown —
    without stopping the withdrawals at the others: a withdrawal lifts nothing,
    so it is safe to write without the whole picture. Run again, it reads
    afresh and writes only what still stands.

    ``report`` collects one line per withdrawal written. Returns whether
    nothing the community collected still stands anywhere it looked.
    """
    if not settings.ds_connector_url:
        return False
    if not submission.dataspace_did:
        return False

    # Same reason as the grant: the manifest names the connectors, so a stale
    # cache would leave a holder unread.
    await template_service.ensure_fresh()
    binding = template_service.dataspace_binding(submission.rec_slug)
    subject_id = submission.dataspace_did
    why = withdrawal_reason(reason or f"Membership revoked in {submission.rec_slug}")

    own = ConsentRoute(
        offer_id="",
        connector_url=settings.ds_connector_url.rstrip("/"),
        collector=binding.organization,
    )
    connectors = [own] + [
        ConsentRoute(
            offer_id="",
            connector_url=c.url.rstrip("/"),
            collector=binding.organization,
            holder=c.holder,
        )
        for c in binding.connectors
        if c.url.rstrip("/") != own.connector_url
    ]

    failures: list[str] = []
    try:
        community = await _community_did(binding)
    except Exception as exc:  # noqa: BLE001 — reported, and nothing written
        failures.append(str(exc))
        connectors = []

    async with httpx.AsyncClient(timeout=30) as client:
        for connector in connectors:
            try:
                rows = await _subject_rows(client, connector, subject_id=subject_id)
            except Exception as exc:  # noqa: BLE001 — reported for this connector
                failures.append(
                    f"could not read what {connector.where} records for this member, so "
                    f"what stands there is unknown: {exc}"
                )
                continue
            offers = dict.fromkeys(
                str(row["offer_id"])
                for row in rows
                if _collected_here(row, community=community, own_connector=connector is own)
            )
            for offer_id in offers:
                route = ConsentRoute(
                    offer_id=offer_id,
                    connector_url=connector.connector_url,
                    collector=connector.collector,
                    holder=connector.holder,
                )
                registration = await register_share(
                    client,
                    route,
                    subject_id=subject_id,
                    enabled=False,
                    decided_by="collector",
                    reason=why,
                )
                if not registration.ok:
                    failures.append(f"{offer_id}: {registration.detail}")
                elif report is not None:
                    report.append(f"{offer_id} withdrawn at {route.where}")

    if failures:
        logger.error("Withdrawing shares for %s failed: %s", submission.ref, "; ".join(failures))
        if raise_on_error:
            raise ValueError("Share withdrawal failed: " + "; ".join(failures))
        return False

    submission.share_provisioned = False
    return True


async def _register_membership(
    base_url: str,
    headers: dict[str, str],
    did: str,
    org_alias: str,
) -> None:
    """Register the user DID as a member of the REC organization.

    Membership is what the ds consent endpoints check, so a user without it holds a
    valid credential but cannot manage data sharing.

    **A membership says where somebody belongs, not what they are there**, and
    that is why no role is sent. The registry used to accept one, store it in a
    column nothing read, and has since dropped the column; what a person is in a
    community is a ``communityRole`` claim on their data-subject credential,
    changed by reissuing it. Sending a role here recorded nothing while reading,
    to anybody who found it, as though it recorded something.

    Onboarding does **not** create the organization. Dataspace trust state arrives
    through the registry's verify -> agreement -> credential -> promote chain,
    seeded from the deployment's owners.yaml by an operator. An organization
    created here would carry no verification, no agreement and therefore no
    declared capacity — and capacity is what the connector's circle check reads to
    decide whether a party is a processor or an independent controller. A 404 is
    a deployment error to fix in the registry, not something an approval papers
    over.
    """
    body = {
        "user_did": did,
        "organization_alias": org_alias,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f"{base_url}/admin/memberships", json=body, headers=headers)

    if resp.status_code == 409:
        logger.info("Membership for %s in %s already exists", did, org_alias)
        return
    if resp.status_code == 404:
        raise ValueError(
            f"Dataspace organization {org_alias!r} does not exist in the identity "
            "registry. It must be seeded and promoted by an operator from the "
            "deployment's owners.yaml before members can be onboarded; onboarding "
            "deliberately does not create it."
        )
    if resp.status_code >= 400:
        raise ValueError(f"Membership registration failed ({resp.status_code}): {resp.text}")


async def _delete_membership(
    base_url: str, headers: dict[str, str], did: str, org_alias: str
) -> None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.delete(f"{base_url}/admin/memberships/{did}/{org_alias}", headers=headers)
    except Exception:
        logger.exception("Failed to delete membership %s/%s during rollback", did, org_alias)


def _warn_if_partial_sync(resp: httpx.Response, did: str) -> None:
    try:
        body = resp.json()
    except ValueError:
        return
    if body.get("keycloak_attribute_synced") is False or body.get("status") == "partial":
        logger.warning(
            "Keycloak sync for %s is partial: %s",
            did,
            body.get("warning", "dataspace_did attribute may be missing on the KC user"),
        )


class KeycloakSyncError(ValueError):
    """The identity registry would not write the DID <-> Keycloak mapping.

    ``status_code`` is the registry's last answer, or ``None`` when it could not
    be reached. A ``409`` is another DID already bound to the Keycloak user.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


async def sync_keycloak_mapping(
    base_url: str,
    headers: dict[str, str],
    *,
    did: str,
    keycloak_user_id: str,
    keycloak_realm: str,
    email: str | None,
    username: str | None = None,
) -> None:
    """Write the registry's mapping from a DID to its Keycloak user, with retries.

    The call alone, with nothing undone on failure: approval wraps it in
    :func:`_sync_keycloak`, which rolls the credential back, and a correction of
    the member's email (``services/propagation.py``) re-syncs the same DID with
    the new address, where there is nothing to roll back. The registry keeps the
    DID on an email change and overwrites ``username`` only when one is sent.

    Raises :class:`KeycloakSyncError` after ``_KC_SYNC_MAX_RETRIES`` attempts.
    """
    sync_body = {
        "did": did,
        "keycloak_realm": keycloak_realm,
        "keycloak_user_id": keycloak_user_id,
    }
    if email:
        sync_body["email"] = email
    if username:
        sync_body["username"] = username

    last_error: Exception | None = None
    last_status: int | None = None
    for attempt in range(1, _KC_SYNC_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{base_url}/admin/keycloak/sync",
                    json=sync_body,
                    headers=headers,
                )
                if resp.status_code < 400:
                    # A 2xx with status "partial" means the DID mapping was
                    # stored but the dataspace_did attribute push to Keycloak
                    # failed. That is retriable and does not orphan the
                    # credential, so we accept it but surface it for operators.
                    _warn_if_partial_sync(resp, did)
                    return
                last_status = resp.status_code
                last_error = ValueError(f"KC sync failed ({resp.status_code}): {resp.text}")
        except httpx.HTTPError as exc:
            last_error = exc

        if attempt < _KC_SYNC_MAX_RETRIES:
            logger.warning("KC sync attempt %d/%d failed, retrying", attempt, _KC_SYNC_MAX_RETRIES)

    raise KeycloakSyncError(
        f"Keycloak sync failed after {_KC_SYNC_MAX_RETRIES} attempts", status_code=last_status
    ) from last_error


async def _sync_keycloak(
    base_url: str,
    headers: dict[str, str],
    *,
    did: str,
    keycloak_user_id: str,
    keycloak_realm: str,
    email: str | None,
    username: str | None = None,
    credential_id: str,
    organization_alias: str = "",
) -> None:
    """Put the DID on the Keycloak user, and the username beside it.

    Fixes [#2](https://github.com/celine-eu/onboarding/issues/2).

    **The username is what the data plane joins on.** A dataspace decision names
    people by DID; the systems holding their data do not. The connector
    translates one to the other through this registry
    (`POST /users/identities` → `KeycloakMapping.username or .email`) and hands
    the answer to the celine `dataset-api`, which resolves it against
    `Member.user_id` — the value :func:`rec_registry.member_user_id` writes from
    the same provisioning result this argument comes from. Sending the username
    is what keeps both ends naming a person the same way.

    **Email is the registry's fallback, not a substitute.** Omitting the username
    leaves the connector resolving subjects by email, which is right only while
    username == email. That is this service's own convention for users it
    creates and explicitly not the platform's: `_find_user` also adopts a user
    whose username is something else, and for them the two ends would disagree —
    the row filter resolves nobody, the handler denies, and a person who
    consented silently gets no rows.
    """
    try:
        await sync_keycloak_mapping(
            base_url,
            headers,
            did=did,
            keycloak_user_id=keycloak_user_id,
            keycloak_realm=keycloak_realm,
            email=email,
            username=username,
        )
        return
    except KeycloakSyncError as exc:
        failure = exc

    logger.error(
        "KC sync failed after %d attempts, revoking credential %s",
        _KC_SYNC_MAX_RETRIES,
        credential_id,
    )
    if organization_alias:
        await _delete_membership(base_url, headers, did, organization_alias)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.delete(
                f"{base_url}/admin/credentials/{credential_id}",
                headers=headers,
            )
    except Exception:
        logger.exception("Failed to revoke credential %s during rollback", credential_id)

    raise ValueError(
        f"Keycloak sync failed after {_KC_SYNC_MAX_RETRIES} attempts; "
        f"credential {credential_id} has been revoked"
    ) from (failure.__cause__ or failure)


async def revoke_user_identity(submission: Submission) -> str:
    """Undo a dataspace identity: membership first, then the credential.

    That order matters for the same reason issuance runs the other way — the
    membership has a foreign key to the DID, so deleting the credential first
    would leave a membership pointing at nothing.

    The submission's identity columns are cleared on success, which is what makes
    a subsequent re-approval issue a fresh credential rather than short-circuit on
    a `dataspace_vc_id` that no longer resolves.
    """
    credential_id = submission.dataspace_vc_id
    did = submission.dataspace_did
    if not credential_id or not did:
        return "no dataspace credential recorded"

    if not settings.identity_registry_url:
        raise ValueError("IDENTITY_REGISTRY_URL is required to revoke an identity")

    base_url = settings.identity_registry_url.rstrip("/")
    headers = await _auth_headers()

    await template_service.ensure_fresh()
    binding = template_service.dataspace_binding(submission.rec_slug)
    if binding.organization:
        await _delete_membership(base_url, headers, did, binding.organization)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(f"{base_url}/admin/credentials/{credential_id}", headers=headers)
        # 404 is success for this purpose: the credential is gone either way, and
        # refusing to clear the local columns would make the state unrepairable.
        if resp.status_code >= 400 and resp.status_code != 404:
            raise ValueError(f"Credential revocation failed ({resp.status_code}): {resp.text}")

    submission.dataspace_vc_id = None
    submission.dataspace_did = None
    submission.dataspace_vc_issued_at = None
    submission.share_provisioned = False
    return f"revoked credential {credential_id}"
