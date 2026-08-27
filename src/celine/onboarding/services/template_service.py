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
        result = await db.execute(select(Rec).where(Rec.active == True))
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
        result.append({
            "slug": slug,
            "name": manifest.get("name", slug),
            "locale": manifest.get("locale", "it"),
            "branding": manifest.get("branding", {}),
        })
    return result


def load_manifest(rec_slug: str) -> dict[str, Any]:
    if rec_slug not in _cache:
        raise KeyError(f"REC '{rec_slug}' not found")
    return _cache[rec_slug]


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


def _templates_dir() -> Path:
    p = Path(settings.templates_dir)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p


def template_dir_for(rec_slug: str) -> Path:
    return _templates_dir() / rec_slug


def get_config(rec_slug: str) -> dict[str, Any]:
    manifest = load_manifest(rec_slug)
    return {
        "slug": manifest.get("slug", rec_slug),
        "name": manifest.get("name", "REC Onboarding"),
        "locale": manifest.get("locale", "it"),
        "branding": manifest.get("branding", {}),
        "fields": manifest.get("fields", {"extra": [], "hidden": []}),
        "consent": manifest.get("consent", {}),
        "steps": manifest.get("steps", ["consents", "personal", "review"]),
        "content": _load_content(rec_slug, manifest),
    }


class SharingOffersUnavailable(RuntimeError):
    """The published offers vocabulary could not be read.

    Distinct from "this community offers nothing to share", which is an empty
    list. Conflating the two is how a misconfigured vocabulary costs every
    consent in the window without anyone noticing.
    """


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
    membership_role: str = "member"

    @property
    def enabled(self) -> bool:
        """Whether this REC participates in the dataspace at all.

        A REC without a block runs the full wizard and provisions no dataspace
        identity — supported, not degraded, since onboarding must keep working
        with no dataspace infrastructure at all.
        """
        return bool(self.organization)


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


def dataspace_binding(rec_slug: str) -> DataspaceBinding:
    """Resolve a REC's dataspace binding from its manifest."""
    block = load_manifest(rec_slug).get("dataspace")
    if not block:
        return DataspaceBinding()

    validate_dataspace_block(block, where=f"REC {rec_slug!r}")
    return DataspaceBinding(
        organization=str(block["organization"]).strip(),
        organization_did=str(block.get("organization_did", "") or "").strip(),
        linked_participant_did=str(
            block.get("linked_participant_did", "") or ""
        ).strip(),
        membership_role=str(block.get("membership_role") or "member").strip(),
    )


@dataclass(frozen=True)
class RecRegistryBinding:
    """Where a REC's approved participants are registered as community members.

    Per-REC for the same reason the dataspace binding is: one deployment serves
    several communities, and each is its own community in the registry.

    ``areas`` maps each registry area key to the municipalities it covers —
    a coarse stand-in for the community's real geofences, authored the same way
    the manifest's ``coverage.rules`` already are:

    .. code-block:: yaml

        areas:
          valley-north: [Springfield, Shelbyville]
          valley-south: [Ogdenville]

    Broad is the point. Matching a municipality is not the same as resolving a
    point against a polygon, and it will be wrong for a member whose supply
    address sits in a municipality split across two areas. It is right often
    enough to be worth doing, and a REC manager moves the rest — which is why
    ``default_area`` is required rather than optional. A member with no area at
    all could not be registered; a member in the wrong one is visible and
    movable.
    """

    community: str = ""
    default_area: str = ""
    areas: dict[str, list[str]] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self.community)

    def area_for(self, municipality: str | None) -> str:
        """The area covering *municipality*, or the default.

        Case- and whitespace-insensitive, because the name arrives from OCR of a
        utility bill rather than from a picker.
        """
        if not municipality:
            return self.default_area

        needle = municipality.strip().casefold()
        for area_key, municipalities in self.areas.items():
            if any(m.strip().casefold() == needle for m in municipalities):
                return area_key
        return self.default_area


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
                f"{where}: 'rec_registry.areas.{area_key}' must be a list of "
                "municipality names"
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


def rec_registry_binding(rec_slug: str) -> RecRegistryBinding:
    """Resolve a REC's registry binding from its manifest."""
    block = load_manifest(rec_slug).get("rec_registry")
    if not block:
        return RecRegistryBinding()

    validate_rec_registry_block(block, where=f"REC {rec_slug!r}")
    return RecRegistryBinding(
        community=str(block["community"]).strip(),
        default_area=str(block["default_area"]).strip(),
        areas={
            str(k): [str(m) for m in v] for k, v in (block.get("areas") or {}).items()
        },
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
        raise SharingOffersUnavailable("No sharing-offers vocabulary is configured")

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
        raise SharingOffersUnavailable(
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
        result.append(offer)
    return result


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
