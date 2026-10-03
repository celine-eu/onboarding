import copy
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from celine.onboarding.config.settings import REPO_ROOT, settings

logger = logging.getLogger(__name__)

_cache: dict[str, dict[str, Any]] = {}
_cache_loaded_at: float = 0.0
CACHE_TTL: float = 5.0

# The organisation alias is one identifier across the whole platform: the owner
# `id` in the deployment's owners.yaml, the Keycloak organization alias, and the
# identity-registry owner id. This pattern mirrors the owners schema exactly —
# including the single-character form — so a value that is valid there cannot be
# rejected here.
SAFE_ORG_ALIAS = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$")


async def load_recs_from_db() -> None:
    global _cache_loaded_at
    from sqlalchemy import select

    from celine.onboarding.models.database import async_session
    from celine.onboarding.models.rec import Rec

    async with async_session() as db:
        result = await db.execute(select(Rec).where(Rec.active.is_(True)))
        recs = result.scalars().all()
        _cache.clear()
        for rec in recs:
            _cache[rec.slug] = rec.manifest
    _cache_loaded_at = time.monotonic()


async def ensure_fresh() -> None:
    if time.monotonic() - _cache_loaded_at > CACHE_TTL:
        await load_recs_from_db()


def get_slugs() -> list[str]:
    return [slug for slug, manifest in _cache.items() if manifest]


def get_all_recs_summary() -> list[dict[str, Any]]:
    result = []
    for slug, manifest in _cache.items():
        if not manifest:
            continue
        result.append(
            {
                "slug": slug,
                "name": manifest.get("name", slug),
                "locale": manifest.get("locale", "it"),
                "branding": manifest.get("branding", {}),
            }
        )
    return result


def load_manifest(rec_slug: str) -> dict[str, Any]:
    if rec_slug not in _cache:
        raise KeyError(f"REC '{rec_slug}' not found")
    return _cache[rec_slug]


#: The documents an applicant may be asked to accept at the first step.
CONSENT_SLOTS = ("gdpr", "policy", "statute")


def consent_documents(rec_slug: str) -> dict[str, dict[str, Any]]:
    """Per asked consent slot, its document: url, version (`legal_documents`)."""
    from celine.onboarding.services import legal_documents

    return legal_documents.consent_documents(rec_slug, load_manifest(rec_slug))


def consent_slots(rec_slug: str) -> tuple[str, ...]:
    """The consent slots this community asks for: those it declares and that have a document.

    A community that publishes no statute (or no regulations) leaves the slot out of
    `consent:`, or the legal host has no such document for it; either way the applicant
    is not asked to accept a document nobody can read.
    """
    documents = consent_documents(rec_slug)
    return tuple(slot for slot in CONSENT_SLOTS if slot in documents)


# ---------------------------------------------------------------------------
# Organisation — the tenancy key for the admin console
# ---------------------------------------------------------------------------


def validate_organization(manifest: dict[str, Any], *, where: str) -> None:
    """Reject a malformed or contradictory top-level ``organization:``.

    Optional. A REC without one is administrable only by **platform** operators
    (realm-level groups); nobody can be granted access to it per community. That
    is a coherent setup for a single-community deployment, and it fails closed —
    no organisation means no organisation-scoped grant matches.
    """
    if "organization" not in manifest:
        return

    alias = str(manifest.get("organization") or "").strip()
    if not alias:
        raise ValueError(
            f"{where}: 'organization' is present but empty. Omit the key entirely "
            "if this community has no Keycloak organization; leaving it blank "
            "reads as a value that failed to interpolate."
        )
    if not SAFE_ORG_ALIAS.fullmatch(alias):
        raise ValueError(
            f"{where}: 'organization' must be lowercase alphanumeric with inner "
            f"hyphens (got {alias!r}). It is the Keycloak organization alias."
        )

    # AGENTS.md commits to these being one identifier. Enforce it where the
    # author is already looking, rather than letting an operator authenticate
    # against one name while their members are filed under another.
    dataspace = manifest.get("dataspace")
    if isinstance(dataspace, dict):
        ds_alias = str(dataspace.get("organization") or "").strip()
        if ds_alias and ds_alias != alias:
            raise ValueError(
                f"{where}: 'organization' ({alias!r}) and "
                f"'dataspace.organization' ({ds_alias!r}) disagree. These are one "
                "identifier — the Keycloak organization alias, the identity "
                "registry owner id and the owners.yaml id are the same string."
            )


def organization_for(rec_slug: str) -> str:
    """The Keycloak organization alias that owns *rec_slug*, or ``""``.

    Falls back to ``dataspace.organization`` so that a community already bound to
    the dataspace does not have to restate the same alias: they are the same
    identifier by definition, and `validate_organization` refuses a manifest where
    they disagree.
    """
    manifest = load_manifest(rec_slug)
    alias = str(manifest.get("organization") or "").strip()
    if alias:
        return alias

    dataspace = manifest.get("dataspace")
    if isinstance(dataspace, dict):
        return str(dataspace.get("organization") or "").strip()
    return ""


def recs_for_organization(alias: str) -> list[str]:
    """Slugs of every active REC owned by *alias*.

    Resolved from the manifest cache rather than SQL: the manifest is the source
    of truth and is already in memory. Should a query ever need this in the
    database, it is ``recs.manifest->>'organization'``.
    """
    if not alias:
        return []
    return [slug for slug in get_slugs() if organization_for(slug) == alias]


def registry_community_for(rec_slug: str) -> str:
    """The manifest's ``rec_registry.community``, or ``""`` when it declares none.

    Read leniently, without :func:`rec_registry_binding`'s validation: resolving
    one community must not fail because another REC's block is malformed. That
    block is refused where it is used.
    """
    block = load_manifest(rec_slug).get("rec_registry")
    if not isinstance(block, dict):
        return ""
    return str(block.get("community") or "").strip()


def recs_for_registry_community(community: str) -> list[str]:
    """Slugs of every active REC whose members the registry files under *community*.

    The reverse of :func:`registry_community_for`, as :func:`recs_for_organization`
    is of :func:`organization_for`. More than one is an authoring error, and the
    caller says so rather than choosing: two manifests filing into one registry
    community would give one member two sets of operators.
    """
    if not community:
        return []
    return [slug for slug in get_slugs() if registry_community_for(slug) == community]


def _templates_dir() -> Path:
    p = Path(settings.templates_dir)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p


def template_dir_for(rec_slug: str) -> Path:
    return _templates_dir() / rec_slug


def _consent_config(rec_slug: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """The manifest's `consent:` with each document slot resolved (`legal_documents`).

    A slot that is not asked is left out, and the data-sharing block gains the notice
    shown above the offers when there is one.
    """
    from celine.onboarding.services import legal_documents

    consent = {k: v for k, v in (manifest.get("consent") or {}).items() if k not in CONSENT_SLOTS}
    consent.update(legal_documents.consent_documents(rec_slug, manifest))
    notice = legal_documents.data_sharing_notice(rec_slug, manifest)
    if notice and isinstance(consent.get("data_sharing"), dict):
        consent["data_sharing"] = {**consent["data_sharing"], "notice": notice}
    return consent


def get_config(rec_slug: str) -> dict[str, Any]:
    manifest = load_manifest(rec_slug)
    return {
        "slug": manifest.get("slug", rec_slug),
        "name": manifest.get("name", "REC Onboarding"),
        "locale": manifest.get("locale", "it"),
        "branding": manifest.get("branding", {}),
        "fields": manifest.get("fields", {"extra": [], "hidden": []}),
        "consent": _consent_config(rec_slug, manifest),
        "steps": manifest.get("steps", list(DEFAULT_STEPS)),
        "existing_members": existing_members(manifest),
        "content": _load_content(rec_slug, manifest),
    }


class SharingOffersUnavailableError(RuntimeError):
    """The published offers vocabulary could not be read.

    Distinct from "this community offers nothing to share", which is an empty
    list. Conflating the two is how a misconfigured vocabulary costs every
    consent in the window without anyone noticing.
    """


@dataclass(frozen=True)
class ConnectorBinding:
    """Another participant's connector, and the offers it holds the data for.

    A consent is recorded at the connector that **serves the data**, which is not
    always the community's own: a grid operator holds its members' meter
    readings, and the decision to release them has to reach the grid operator's
    connector or it enforces nothing. The community is the *collector* — the
    member's relationship is with it — and the holder accepts its registrations
    because it has recorded the community as an accepted consent collector.

    ``holder`` is the owner alias of the participant whose connector this is, in
    the same one-identifier sense as :attr:`DataspaceBinding.organization`. It is
    carried so a reader (and a log line) can say *whose* connector refused,
    without resolving a URL back to a party.

    ``offers`` are the offer ids routed here. An offer named by no entry stays at
    the community's own connector — that is the ordinary case, and the absence of
    this block is a community whose data is all its own. An offer may be named by
    several entries: its data is then held in several places, and the decision is
    recorded at each (ADR-0007).
    """

    holder: str
    url: str
    offers: tuple[str, ...] = ()


@dataclass(frozen=True)
class DataspaceBinding:
    """Which dataspace organisation a REC's approved members belong to.

    Per-REC, because this platform is multi-tenant: manifests live in the ``Rec``
    table and every submission carries a ``rec_slug``, so a deployment serving two
    communities must not file both into one organisation. It previously read from
    global environment variables, which did exactly that — silently, since the
    wrong membership is still a successful ``201``.

    ``organization`` is the owner alias in the identity registry, which is also
    the Keycloak organization alias and the owner ``id`` in the deployment's
    owners.yaml. One identifier, no mapping table.
    """

    organization: str = ""
    organization_did: str = ""
    linked_participant_did: str = ""
    #: Other participants' connectors, by the offers they hold. See
    #: :class:`ConnectorBinding`. Never the community's own: an entry naming it
    #: lands in :attr:`own_offers` instead.
    connectors: tuple[ConnectorBinding, ...] = ()
    #: Offers another participant holds data for **and** the community holds data
    #: for too — the manifest's entry whose ``holder`` is this community's own
    #: alias. Only meaningful for an offer some other entry names: one named by
    #: nobody is recorded here anyway.
    own_offers: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        """Whether this REC participates in the dataspace at all.

        A REC without a block runs the full wizard and provisions no dataspace
        identity — supported, not degraded, since onboarding must keep working
        with no dataspace infrastructure at all.
        """
        return bool(self.organization)

    def connectors_for(self, offer_id: str) -> tuple[ConnectorBinding, ...]:
        """Every other participant holding data this offer reaches, in manifest order.

        Empty means the community's own connector (``DS_CONNECTOR_URL``) alone, and
        it is the answer for every offer nobody routed — so a deployment with no
        ``connectors:`` block behaves exactly as it did before routing existed.

        The map is configuration and never inferred from the offer. An offer's
        ``recipients.recipient`` names **who the data goes to**, which since ds's
        rename is emphatically not who holds it: the release offer's recipient is
        the community itself, and its data sits at the grid operator. Deriving
        the route from the recipient would send every release decision to the
        connector that does not serve the rows.
        """
        return tuple(c for c in self.connectors if offer_id in c.offers)

    def recorded_here(self, offer_id: str) -> bool:
        """Whether this offer's decision is (also) recorded at the community's own connector.

        Yes for an offer nobody routed, and for one the manifest's own entry names
        beside another participant. No only for an offer whose data is entirely
        somebody else's.
        """
        return offer_id in self.own_offers or not self.connectors_for(offer_id)


def validate_dataspace_block(block: Any, *, where: str) -> None:
    """Reject a malformed ``dataspace:`` block, loudly and early.

    Called from template import so a bad alias fails where an operator is already
    looking, rather than the first time a REC manager approves somebody.
    """
    if block is None:
        return
    if not isinstance(block, dict):
        raise ValueError(f"{where}: 'dataspace' must be a mapping")

    alias = str(block.get("organization", "")).strip()
    if not alias:
        # A credential without a membership is an identity that cannot do
        # anything: the consent endpoints gate on membership. There is no reason
        # to express it, so it is not expressible.
        raise ValueError(
            f"{where}: 'dataspace.organization' is required. Omit the whole "
            "'dataspace' block to keep this community out of the dataspace."
        )
    if not SAFE_ORG_ALIAS.fullmatch(alias):
        raise ValueError(
            f"{where}: 'dataspace.organization' must be lowercase alphanumeric "
            f"with inner hyphens (got {alias!r}). It must match the owner id in "
            "the deployment's owners.yaml exactly."
        )

    for key in ("organization_did", "linked_participant_did"):
        did = str(block.get(key, "")).strip()
        if did and not did.startswith("did:"):
            raise ValueError(f"{where}: 'dataspace.{key}' must be a DID (got {did!r})")

    _validate_connectors(block.get("connectors"), where=where, own_alias=alias)


def _validate_connectors(block: Any, *, where: str, own_alias: str = "") -> None:
    """Refuse a malformed ``dataspace.connectors:`` block, at import.

    Every failure here is a consent recorded at the wrong connector or at none,
    and both are silent: the member sees a granted toggle either way. So the
    block is checked where an operator is already looking rather than at the
    first approval.

    **An offer may be held in several places** (ADR-0007): each entry naming it
    is a connector its decision is recorded at. An entry whose ``holder`` is the
    community's own alias (``own_alias``) says the community holds data for those
    offers too; it carries **no** ``url``, because that address is
    ``DS_CONNECTOR_URL`` and two homes for one address disagree some day.

    Still refused, because each is two answers to one question: one holder in two
    entries (which URL is it?), and one offer twice in an entry.
    """
    if block is None:
        return
    if not isinstance(block, list):
        raise ValueError(f"{where}: 'dataspace.connectors' must be a list")

    holders: set[str] = set()
    for index, entry in enumerate(block):
        at = f"{where}: 'dataspace.connectors[{index}]'"
        if not isinstance(entry, dict):
            raise ValueError(f"{at} must be a mapping")

        holder = str(entry.get("holder", "")).strip()
        if not holder:
            raise ValueError(
                f"{at} has no 'holder'. Name the participant whose connector this "
                "is, by the owner alias — the same identifier as "
                "'dataspace.organization'."
            )
        if not SAFE_ORG_ALIAS.fullmatch(holder):
            raise ValueError(
                f"{at}: 'holder' must be lowercase alphanumeric with inner hyphens (got {holder!r})"
            )
        if holder in holders:
            raise ValueError(
                f"{at}: holder {holder!r} already has an entry. One participant has "
                "one connector; list all of its offers in one entry."
            )
        holders.add(holder)

        url = str(entry.get("url", "") or "").strip()
        if own_alias and holder == own_alias:
            if url:
                raise ValueError(
                    f"{at}: {holder!r} is this community itself, whose connector is "
                    "DS_CONNECTOR_URL. Omit 'url' — two homes for one address "
                    "disagree some day."
                )
        elif not url.startswith(("http://", "https://")):
            raise ValueError(
                f"{at}: 'url' must be the holder's connector base URL, http(s) (got {url!r})"
            )

        offers = entry.get("offers")
        if not isinstance(offers, list) or not offers:
            raise ValueError(
                f"{at}: 'offers' must be a non-empty list of offer ids. A "
                "connector routing nothing routes nothing — omit the entry."
            )
        seen: set[str] = set()
        for offer_id in offers:
            if not isinstance(offer_id, str) or not offer_id.strip():
                raise ValueError(f"{at}: every entry in 'offers' must be an offer id")
            offer_id = offer_id.strip()
            if offer_id in seen:
                raise ValueError(f"{at}: offer {offer_id!r} is listed twice")
            seen.add(offer_id)


def dataspace_binding(rec_slug: str) -> DataspaceBinding:
    """Resolve a REC's dataspace binding from its manifest."""
    block = load_manifest(rec_slug).get("dataspace")
    if not block:
        return DataspaceBinding()

    validate_dataspace_block(block, where=f"REC {rec_slug!r}")
    organization = str(block["organization"]).strip()
    entries = block.get("connectors") or []
    return DataspaceBinding(
        organization=organization,
        organization_did=str(block.get("organization_did", "") or "").strip(),
        linked_participant_did=str(block.get("linked_participant_did", "") or "").strip(),
        connectors=tuple(
            ConnectorBinding(
                holder=str(entry["holder"]).strip(),
                url=str(entry["url"]).strip().rstrip("/"),
                offers=tuple(str(o).strip() for o in entry["offers"]),
            )
            for entry in entries
            if str(entry["holder"]).strip() != organization
        ),
        own_offers=tuple(
            str(o).strip()
            for entry in entries
            if str(entry["holder"]).strip() == organization
            for o in entry["offers"]
        ),
    )


def offer_recipient(offer: dict[str, Any]) -> str:
    """The owner alias a published offer names as the party the data goes to.

    ``recipients.recipient`` since ds renamed it; ``recipients.controller`` is
    the deprecated spelling, still served by an older connector and still read
    here so an upgrade of the two services need not be simultaneous. ds accepts
    both on input for the same reason.

    The rename was not cosmetic: the old name meant the recipient, the subject's
    home organisation and the GDPR controller at once, and only the first reading
    held in every offer. So this answers *who receives the data* and nothing
    else — in particular it is **not** where the consent is recorded, which is
    :meth:`DataspaceBinding.connectors_for`.
    """
    recipients = offer.get("recipients") or {}
    value = recipients.get("recipient") or recipients.get("controller") or ""
    return str(value).strip()


#: The boundary sources a template may name. A closed set: each value is one
#: table the Digital Twin's boundary fetchers read, and another country's
#: boundaries are a new value here and there, not new code (ADR-0012).
BOUNDARY_SOURCES: frozenset[str] = frozenset({"gse_cabine_primarie"})

#: The Digital Twin accepts a boundary id of 1 to 64 characters.
BOUNDARY_ID_MAX_LENGTH = 64

#: What a registry area key may be when this service writes it: it goes into a
#: registry URL path (`PUT …/areas/{area_key}`) and a 128-character column, so
#: URL-safe characters only, starting with a letter or digit.
AREA_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

#: The longest display ``name`` a boundary area may carry (D57). It is written
#: to the registry area's ``name`` and its topology node's ``name``.
AREA_NAME_MAX_LENGTH = 128

#: The wizard's steps when a manifest names none (see :func:`get_config`).
DEFAULT_STEPS = ("consents", "personal", "review")

#: The wizard step that creates the submission; nothing a step before it checks
#: can be saved (see :func:`validate_boundary_template`).
SUBMISSION_CREATING_STEP = "consents"


@dataclass(frozen=True)
class BoundaryRef:
    """One primary-substation boundary, by source and id — never a shape.

    For ``gse_cabine_primarie`` the id is the substation's ``cod_ac``. The shape
    stays in gold and is read through the Digital Twin; this service holds no
    geometry and no list of codes (ADR-0012).
    """

    source: str
    id: str


@dataclass(frozen=True)
class RecRegistryBinding:
    """Where a REC's approved participants are registered as community members.

    Per-REC for the same reason the dataspace binding is: one deployment serves
    several communities, and each is its own community in the registry.

    A template declares its areas in one of two ways, never both.

    **As primary-substation boundaries** (``boundaries``; ADR-0012, REQ-0001):

    .. code-block:: yaml

        areas:
          north:
            name: North valley          # optional; the key when absent
            boundary: {source: gse_cabine_primarie, id: AC000E00001}

    The optional ``name`` is the area's display name (D57), written to the
    registry area's ``name`` and its topology node's ``name`` by the registry
    sync; ``names`` holds only the areas that give one (see :meth:`area_name`).

    The boundary that contains the supply address decides both eligibility and
    the member's area, through the Digital Twin (ADR-0013). Such a template has
    no municipality lists and no ``default_area``: there is no fallback.

    **As municipality lists** (``areas``), for a template that declares no
    boundaries — a coarse stand-in, authored the way the manifest's
    ``coverage.rules`` are:

    .. code-block:: yaml

        areas:
          valley-north: [Springfield, Shelbyville]
          valley-south: [Ogdenville]

    Matching a municipality is not resolving a point against a polygon, and it
    is wrong for a member whose supply address sits in a municipality split
    across two areas; a REC manager moves the rest, which is why
    ``default_area`` is required on this path. A member with no area at all
    could not be registered; a member in the wrong one is visible and movable.
    """

    community: str = ""
    default_area: str = ""
    areas: dict[str, list[str]] = field(default_factory=dict)
    boundaries: dict[str, BoundaryRef] = field(default_factory=dict)
    #: Boundary area key -> the display name the template gives it (D57).
    names: dict[str, str] = field(default_factory=dict)

    def area_name(self, area_key: str) -> str:
        """The area's display name: the template's ``name``, or the key itself."""
        return self.names.get(area_key) or area_key

    @property
    def enabled(self) -> bool:
        return bool(self.community)

    @property
    def uses_boundaries(self) -> bool:
        """Whether areas, and eligibility, are decided by boundary."""
        return bool(self.boundaries)

    @property
    def boundary_sources(self) -> tuple[str, ...]:
        """Every source the template's boundaries name, in a stable order."""
        return tuple(sorted({ref.source for ref in self.boundaries.values()}))

    def area_for_boundary(self, source: str | None, boundary_id: str | None) -> str | None:
        """The area whose boundary is exactly this one, or ``None``.

        An exact match on source and id: the id is a code, not a name, so there
        is no case or padding to forgive.
        """
        if not source or not boundary_id:
            return None
        for area_key, ref in self.boundaries.items():
            if ref.source == source and ref.id == boundary_id:
                return area_key
        return None

    def area_for(self, municipality: str | None) -> str:
        """The area covering *municipality*, or the default.

        Case- and whitespace-insensitive, because the name arrives from OCR of a
        utility bill rather than from a picker.

        Refused for a template with boundaries: its area comes from the boundary
        the supply address falls in, and a municipality or a default would be the
        fallback ADR-0013 rules out.
        """
        if self.uses_boundaries:
            raise ValueError(
                "This community's areas are primary-substation boundaries; a member's "
                "area is the boundary their supply address falls in, never a municipality"
            )
        if not municipality:
            return self.default_area

        needle = municipality.strip().casefold()
        for area_key, municipalities in self.areas.items():
            if any(m.strip().casefold() == needle for m in municipalities):
                return area_key
        return self.default_area


def declares_boundaries(block: Any) -> bool:
    """Whether a ``rec_registry`` block declares any area as a boundary.

    Read leniently, so a caller can branch before validation says why a block is
    malformed: one area given as a mapping is enough.
    """
    if not isinstance(block, dict):
        return False
    areas = block.get("areas")
    return isinstance(areas, dict) and any(isinstance(v, dict) for v in areas.values())


def _validate_boundary_areas(block: dict, *, where: str) -> None:
    """The boundary form of ``rec_registry.areas`` (REQ-0001, REQ-0002)."""
    areas = block["areas"]

    if any(not isinstance(v, dict) for v in areas.values()):
        raise ValueError(
            f"{where}: 'rec_registry.areas' mixes boundaries with municipality lists. "
            "A template declares every area as a boundary, or none."
        )
    if str(block.get("default_area") or "").strip():
        raise ValueError(
            f"{where}: 'rec_registry.default_area' is not allowed beside boundaries. "
            "A member's area is the boundary their supply address falls in; there is "
            "no fallback."
        )

    seen: dict[tuple[str, str], str] = {}
    for area_key, entry in areas.items():
        at = f"{where}: 'rec_registry.areas.{area_key}'"
        if not isinstance(area_key, str) or not AREA_KEY.fullmatch(area_key):
            raise ValueError(
                f"{at}: {area_key!r} is not a valid registry area key — letters, digits, "
                "'-' and '_', starting with a letter or digit, at most 128 characters"
            )
        extra = set(entry) - {"boundary", "name"}
        if extra:
            raise ValueError(f"{at}: unknown key(s) {', '.join(sorted(map(str, extra)))}")
        if "name" in entry:
            name = entry["name"]
            if (
                not isinstance(name, str)
                or not name.strip()
                or name != name.strip()
                or len(name) > AREA_NAME_MAX_LENGTH
            ):
                raise ValueError(
                    f"{at}.name must be the area's display name, a string of 1 to "
                    f"{AREA_NAME_MAX_LENGTH} characters with no surrounding spaces "
                    f"(got {name!r}); leave it out to use the key"
                )
        boundary = entry.get("boundary")
        if not isinstance(boundary, dict):
            raise ValueError(
                f"{at}.boundary must be a mapping {{source: gse_cabine_primarie, id: <cod_ac>}}"
            )
        extra = set(boundary) - {"source", "id"}
        if extra:
            raise ValueError(f"{at}.boundary: unknown key(s) {', '.join(sorted(map(str, extra)))}")

        source = boundary.get("source")
        if source not in BOUNDARY_SOURCES:
            raise ValueError(
                f"{at}.boundary.source must be one of {', '.join(sorted(BOUNDARY_SOURCES))} "
                f"(got {source!r})"
            )
        boundary_id = boundary.get("id")
        if (
            not isinstance(boundary_id, str)
            or not boundary_id.strip()
            or boundary_id != boundary_id.strip()
            or len(boundary_id) > BOUNDARY_ID_MAX_LENGTH
        ):
            raise ValueError(
                f"{at}.boundary.id must be the boundary's id in {source!r}, a string of 1 "
                f"to {BOUNDARY_ID_MAX_LENGTH} characters with no surrounding spaces "
                f"(got {boundary_id!r})"
            )

        # Two names for one substation are indistinguishable to everything
        # downstream: the pipelines key on the substation, not on the name.
        key = (source, boundary_id)
        if key in seen:
            raise ValueError(
                f"{where}: boundary {boundary_id!r} ({source}) is declared by both "
                f"{seen[key]!r} and {area_key!r}; one boundary is one area"
            )
        seen[key] = area_key


def validate_rec_registry_block(block: Any, *, where: str) -> None:
    """Reject a malformed ``rec_registry:`` block at template import."""
    if block is None:
        return
    if not isinstance(block, dict):
        raise ValueError(f"{where}: 'rec_registry' must be a mapping")

    if not str(block.get("community", "")).strip():
        raise ValueError(
            f"{where}: 'rec_registry.community' is required. Omit the whole "
            "'rec_registry' block to skip registry registration."
        )

    if declares_boundaries(block):
        _validate_boundary_areas(block, where=where)
        return

    if not str(block.get("default_area", "")).strip():
        raise ValueError(
            f"{where}: 'rec_registry.default_area' is required — it is where a "
            "member goes when no area covers their municipality, and a member "
            "with no area cannot be registered at all."
        )

    areas = block.get("areas") or {}
    if not isinstance(areas, dict):
        raise ValueError(
            f"{where}: 'rec_registry.areas' must map an area key to a list of "
            "municipalities, e.g. {valley-north: [Springfield, Shelbyville]}"
        )
    for area_key, municipalities in areas.items():
        if not isinstance(municipalities, list) or not all(
            isinstance(m, str) for m in municipalities
        ):
            raise ValueError(
                f"{where}: 'rec_registry.areas.{area_key}' must be a list of municipality names"
            )

    # A municipality in two areas resolves to whichever is declared first, which
    # is an authoring mistake rather than a policy — say so at import.
    seen: dict[str, str] = {}
    for area_key, municipalities in areas.items():
        for municipality in municipalities:
            key = municipality.strip().casefold()
            if key in seen and seen[key] != area_key:
                raise ValueError(
                    f"{where}: municipality {municipality!r} is claimed by both "
                    f"{seen[key]!r} and {area_key!r}; a member's area would "
                    "depend on declaration order"
                )
            seen[key] = area_key


def validate_boundary_template(manifest: dict[str, Any], *, where: str) -> None:
    """What a template with boundaries may not also say (REQ-0017, D33).

    - **No ``coverage.rules``** (nor the older ``coverage.municipalities``): the
      boundary alone decides eligibility, and a rule set beside it would be a
      second answer that reads as if it applied.
    - **An ``eligibility`` step**: it is where the supply address is checked
      against the boundaries; without it an applicant outside every boundary
      could reach submission.
    - **The ``eligibility`` step after ``consents``**: the checked address is
      saved on the submission, which ``consents`` creates; checked before it,
      the address is lost and the submission has nothing to resolve from.

    A template without boundaries is not affected.
    """
    if not declares_boundaries(manifest.get("rec_registry")):
        return

    coverage = manifest.get("coverage")
    if isinstance(coverage, dict) and (coverage.get("rules") or coverage.get("municipalities")):
        raise ValueError(
            f"{where}: 'coverage' rules are not allowed beside boundary areas. The "
            "boundary the supply address falls in decides eligibility; remove "
            "'coverage.rules' (and 'coverage.municipalities')."
        )

    steps = manifest.get("steps")
    if steps is None:
        steps = list(DEFAULT_STEPS)
    if not isinstance(steps, list) or "eligibility" not in steps:
        raise ValueError(
            f"{where}: a template with boundary areas must include the 'eligibility' "
            "step, where the supply address is checked against the boundaries."
        )

    # The address the eligibility step checks is saved onto the submission, and
    # the submission exists only once the `consents` step has created it. An
    # eligibility step before that checks an address nothing can save, and the
    # boundary is resolved from the saved address at submit and at approval.
    if SUBMISSION_CREATING_STEP not in steps or steps.index("eligibility") < steps.index(
        SUBMISSION_CREATING_STEP
    ):
        raise ValueError(
            f"{where}: a template with boundary areas must place the 'eligibility' step "
            f"after the '{SUBMISSION_CREATING_STEP}' step, which creates the submission "
            "the checked supply address is saved on."
        )


#: The steps a declared existing member may be spared (REQ-0024). Only steps
#: that collect nothing approval needs from the applicant: the consents, the
#: person, the statute with its sharing offers and the review stay.
#: `eligibility` is among them because a declared member's supply address is
#: the operator's to complete from the register, whatever the step asked.
EXISTING_MEMBERS_SKIPPABLE = frozenset({"utility", "phone_verify", "energy", "eligibility"})


def existing_members(manifest: dict[str, Any]) -> dict[str, Any]:
    """The template's ``existing_members`` block as the wizard reads it.

    ``{"enabled": False, "skip_steps": []}`` when the template declares none, so
    a caller never has to tell "absent" from "off".
    """
    block = manifest.get("existing_members") or {}
    if not isinstance(block, dict) or block.get("enabled") is not True:
        return {"enabled": False, "skip_steps": []}
    return {"enabled": True, "skip_steps": list(block.get("skip_steps") or [])}


def existing_members_enabled(rec_slug: str) -> bool:
    """Whether the REC lets an applicant declare they are already a member."""
    return existing_members(load_manifest(rec_slug))["enabled"]


def validate_existing_members(block: Any, *, where: str) -> None:
    """What an ``existing_members`` block may say (REQ-0024).

    ``enabled`` is a boolean; ``skip_steps`` names only steps in
    :data:`EXISTING_MEMBERS_SKIPPABLE`. Anything else is refused at import and at
    boot, rather than read as "off" or quietly skipping a step approval needs.
    """
    if block is None:
        return
    if not isinstance(block, dict):
        raise ValueError(f"{where}: 'existing_members' must be a mapping")
    unknown = set(block) - {"enabled", "skip_steps"}
    if unknown:
        raise ValueError(
            f"{where}: 'existing_members' has unknown keys {sorted(unknown)}; "
            "it takes 'enabled' and 'skip_steps'"
        )
    if not isinstance(block.get("enabled", False), bool):
        raise ValueError(f"{where}: 'existing_members.enabled' must be true or false")
    skip = block.get("skip_steps", [])
    if not isinstance(skip, list) or not all(isinstance(s, str) for s in skip):
        raise ValueError(f"{where}: 'existing_members.skip_steps' must be a list of step names")
    refused = sorted(set(skip) - EXISTING_MEMBERS_SKIPPABLE)
    if refused:
        raise ValueError(
            f"{where}: 'existing_members.skip_steps' may name only "
            f"{sorted(EXISTING_MEMBERS_SKIPPABLE)}, not {refused}: the other steps "
            "collect what a declared member still gives"
        )


def rec_registry_binding(rec_slug: str) -> RecRegistryBinding:
    """Resolve a REC's registry binding from its manifest."""
    block = load_manifest(rec_slug).get("rec_registry")
    if not block:
        return RecRegistryBinding()

    validate_rec_registry_block(block, where=f"REC {rec_slug!r}")
    community = str(block["community"]).strip()
    if declares_boundaries(block):
        return RecRegistryBinding(
            community=community,
            boundaries={
                str(k): BoundaryRef(source=v["boundary"]["source"], id=v["boundary"]["id"])
                for k, v in block["areas"].items()
            },
            names={str(k): v["name"] for k, v in block["areas"].items() if v.get("name")},
        )
    return RecRegistryBinding(
        community=community,
        default_area=str(block["default_area"]).strip(),
        areas={str(k): [str(m) for m in v] for k, v in (block.get("areas") or {}).items()},
    )


async def get_sharing_offers(rec_slug: str) -> list[dict[str, Any]]:
    """Resolve the data-sharing offers a REC's wizard should render.

    Offers are served by the connector (`GET /ns/sharing-offers`) as codes plus
    an English fallback — the wizard composes its own sentences per locale. The
    manifest's optional `consent.data_sharing.offers` is an allow-list; without
    it, every consent-based offer the connector publishes is offered.

    Returns an empty list when the REC has no `data_sharing` block or no
    connector is configured — the step simply does not appear.
    """
    import httpx

    manifest = load_manifest(rec_slug)
    data_sharing = manifest.get("consent", {}).get("data_sharing")
    if data_sharing is None:
        return []

    base = (settings.ds_ns_url or settings.ds_connector_url).rstrip("/")
    if not base:
        # Startup validation refuses this combination, so reaching it means the
        # configuration changed under a running process.
        logger.error(
            "REC %r declares consent.data_sharing but no offers vocabulary is "
            "configured (DS_NS_URL / DS_CONNECTOR_URL); the sharing step will "
            "not be shown",
            rec_slug,
        )
        raise SharingOffersUnavailableError("No sharing-offers vocabulary is configured")

    allow = data_sharing.get("offers")  # None → all consent-based
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{base}/ns/sharing-offers")
            resp.raise_for_status()
            offers = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Fail closed — never render offers from a cached or local copy. The hash
        # of what was shown only means something if the facts came from the
        # published vocabulary. But say so: a silent empty list is
        # indistinguishable from "this community shares nothing", and every
        # consent in that window is one nobody was asked for.
        logger.warning(
            "Sharing offers unavailable for REC %r from %s (%s); the sharing "
            "step will not be shown",
            rec_slug,
            base,
            exc,
        )
        raise SharingOffersUnavailableError(
            "The sharing-offers vocabulary could not be reached"
        ) from exc

    result = []
    for offer in offers:
        if allow is not None:
            if offer.get("id") not in allow:
                continue
        elif not offer.get("requires_consent"):
            # Default set is consent-based offers; an explicit allow-list may
            # still include a contract offer for disclosure.
            #
            # Rendered is not consented, and the two used to be conflated here.
            # The statute step shows a contract-based offer **without a toggle**
            # (`docs/data-sharing.md`) because there is no choice to make — so it
            # belongs in this list, and it must never reach
            # `data_sharing_consent_offer_ids`. Nothing stopped it: the connector
            # refused it at provisioning with a 409 days later, by which time the
            # failure read as the member declining. That is now checked at capture
            # by `submission_service._validate_sharing_offer_ids`.
            continue
        text = _text_for(rec_slug, offer, data_sharing.get("texts") or {})
        result.append({**offer, "text": text} if text else offer)
    return result


_LOCALE_KEY = re.compile(r"^[a-z]{2}$")


def validate_data_sharing_texts(block: Any, *, where: str) -> None:
    """Refuse a malformed ``consent.data_sharing.texts`` block.

    ``{offer_id: {version: str, <locale>: {title: str, body: str}}}``. The words
    are shown to a person as what they consent to, so a half-written entry is
    refused where an operator is looking rather than rendered as a blank card.
    """
    if block is None:
        return
    if not isinstance(block, dict):
        raise ValueError(f"{where}: consent.data_sharing.texts must be a mapping of offer ids")
    for offer_id, entry in block.items():
        at = f"{where}: consent.data_sharing.texts.{offer_id}"
        if not isinstance(entry, dict):
            raise ValueError(f"{at} must be a mapping")
        version = entry.get("version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f"{at} needs a string 'version' (the offer's consent_text_version)")
        locales = {k: v for k, v in entry.items() if k != "version"}
        if not locales:
            raise ValueError(f"{at} has no locale")
        for locale, text in locales.items():
            if not _LOCALE_KEY.match(str(locale)):
                raise ValueError(f"{at}: {locale!r} is not a two-letter locale code")
            if not isinstance(text, dict):
                raise ValueError(f"{at}.{locale} must be a mapping with 'title' and 'body'")
            extra = set(text) - {"title", "body"}
            if extra:
                raise ValueError(f"{at}.{locale}: unknown key(s) {', '.join(sorted(extra))}")
            for key in ("title", "body"):
                value = text.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{at}.{locale} needs a non-empty '{key}'")


def validate_data_sharing_recipients(block: Any, *, where: str) -> None:
    """Refuse a malformed ``consent.data_sharing.recipients`` block.

    ``{recipient_alias: display_name}``: the name the wizard shows beside each
    offer's title, so who gets the data stays visible without opening the
    details. A proper name, not translated. An alias with no entry shows the
    alias itself.
    """
    if block is None:
        return
    if not isinstance(block, dict):
        raise ValueError(f"{where}: consent.data_sharing.recipients must map recipient aliases to names")
    for alias, name in block.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{where}: consent.data_sharing.recipients.{alias} needs a non-empty name")


def validate_data_sharing_summary(block: Any, *, where: str) -> None:
    """Refuse a malformed ``consent.data_sharing.summary`` block.

    ``{<locale>: {title: str, label: str}}``: the one switch that ticks every
    offer, and the heading above it. Its words are shown as part of what a person
    agrees to, so a half-written one is refused rather than rendered blank.
    """
    if block is None:
        return
    if not isinstance(block, dict) or not block:
        raise ValueError(f"{where}: consent.data_sharing.summary must map locales to a title and label")
    for locale, text in block.items():
        at = f"{where}: consent.data_sharing.summary.{locale}"
        if not _LOCALE_KEY.match(str(locale)):
            raise ValueError(f"{at}: {locale!r} is not a two-letter locale code")
        if not isinstance(text, dict):
            raise ValueError(f"{at} must be a mapping with 'title' and 'label'")
        extra = set(text) - {"title", "label"}
        if extra:
            raise ValueError(f"{at}: unknown key(s) {', '.join(sorted(extra))}")
        for key in ("title", "label"):
            value = text.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{at} needs a non-empty '{key}'")


def _text_for(rec_slug: str, offer: dict[str, Any], texts: dict[str, Any]) -> dict[str, Any] | None:
    """The community's wording for *offer*, if it was written for this version.

    A text for another version describes a different offer: showing it would put
    words in front of a member that the published offer no longer matches, so the
    generic fallback is shown instead and the operator is told.
    """
    entry = texts.get(offer.get("id"))
    if not entry:
        return None
    if entry.get("version") != offer.get("consent_text_version"):
        logger.error(
            "REC %r: the text for offer %r was written for version %r but the "
            "connector publishes %r; the text is not shown until both agree",
            rec_slug,
            offer.get("id"),
            entry.get("version"),
            offer.get("consent_text_version"),
        )
        return None
    # A copy: the manifest is a cached row shared by every request.
    return copy.deepcopy(entry)


async def get_sharing_offer(rec_slug: str, offer_id: str) -> dict[str, Any]:
    """One published sharing offer, as this REC is allowed to use it.

    Resolved through :func:`get_sharing_offers` rather than by filtering the
    connector's whole vocabulary, so the REC's manifest allow-list applies: an
    offer this community does not offer is not one it may export under, and the
    two questions have one answer.

    The record is the connector's published projection — the same facts the
    person was shown when they decided. That is what makes it the right source
    for an export's header: coverage, resolution and retention describe what was
    consented to, and a second copy of them anywhere else is a second record of
    the same thing waiting to disagree.

    Raises ``ValueError`` when the offer is not one this REC publishes, and
    :class:`SharingOffersUnavailableError` when the vocabulary cannot be read —
    the same failure the wizard fails closed on, for the same reason.
    """
    offers = await get_sharing_offers(rec_slug)
    for offer in offers:
        if offer.get("id") == offer_id:
            return offer
    raise ValueError(
        f"REC {rec_slug!r} publishes no sharing offer {offer_id!r}"
        + (f" (it publishes: {', '.join(str(o.get('id')) for o in offers)})" if offers else "")
    )


def get_consent_dir(rec_slug: str) -> Path:
    tpl = template_dir_for(rec_slug)
    consent_dir = tpl / "consent"
    if consent_dir.exists():
        return consent_dir
    return Path(settings.data_dir) / "consent"


def get_asset_path(rec_slug: str, relative: str) -> Path | None:
    if ".." in relative or relative.startswith("/"):
        return None
    tpl = template_dir_for(rec_slug)
    path = (tpl / relative).resolve()
    if not path.is_relative_to(tpl.resolve()):
        return None
    if path.exists() and path.is_file():
        return path
    return None


def _load_content(rec_slug: str, manifest: dict) -> dict[str, str]:
    content_map = manifest.get("content", {})
    tpl = template_dir_for(rec_slug)
    result = {}
    for key, filename in content_map.items():
        path = tpl / filename
        if path.exists():
            result[key] = path.read_text(encoding="utf-8").strip()
    return result


async def reload() -> None:
    await load_recs_from_db()
