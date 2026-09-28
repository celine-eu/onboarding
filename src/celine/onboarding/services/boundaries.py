"""Primary-substation boundaries, resolved through the Digital Twin.

For a community whose template declares its areas as boundaries (ADR-0012), one
lookup decides both whether a supply address is inside the community and which
area its member joins (ADR-0013): the geocoder's point goes to the Digital Twin
fetcher ``boundary_at_point``, and the answered id is either the boundary of one
of the template's areas or it is not. ``boundary_shape`` is asked only at
template import, to refuse an id the Digital Twin does not know.

This service holds no geometry and no list of codes. It keeps no coordinates
either: a point is sent to the Digital Twin and dropped, and **it is never
logged** — neither here nor in an exception message, since the refusal body the
Digital Twin answers may echo the payload. What a submission keeps is the
boundary id and its source (REQ-0007).

Every way the Digital Twin can fail to answer — unreachable, too slow, refusing
this service's token, answering something unreadable — is one typed failure,
:class:`DigitalTwinUnavailableError`, and every caller fails closed on it: an
eligibility nobody computed is neither a yes nor a no (REQ-0005).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from celine.onboarding.config.settings import settings
from celine.onboarding.services import template_service
from celine.onboarding.services.template_service import RecRegistryBinding

logger = logging.getLogger(__name__)

#: The Digital Twin's own limit on ``boundary_shape``'s ``ids``.
SHAPE_BATCH = 100

AT_POINT = "boundary_at_point"
SHAPE = "boundary_shape"

#: The registry step's failure code when the supply address's boundary is no
#: area of the template in force at approval (REQ-0008).
BOUNDARY_NOT_IN_COMMUNITY = "boundary_not_in_community"


class DigitalTwinUnavailableError(Exception):
    """The Digital Twin did not give an answer, so nothing was decided.

    The message names the fetcher and the kind of failure, never the payload:
    the payload of ``boundary_at_point`` is somebody's supply point.
    """


class BoundaryUnavailableError(Exception):
    """A supply address's boundary could not be resolved right now.

    The geocoder or the Digital Twin did not answer. Not a ``ValueError``,
    because the routes turn those into ``422`` and nothing about the request is
    wrong: the answer is ``503``, and the same request may succeed later.
    """


class BoundariesUncheckedError(ValueError):
    """Template import could not ask the Digital Twin, so the template is refused.

    A ``ValueError`` like every other refusal of a template, so import treats it
    the same; its own class so the registry sync can answer ``503`` for it
    rather than calling the template invalid.
    """


class BoundaryNotInCommunityError(ValueError):
    """The supply address falls in no boundary the template's areas declare."""

    code = BOUNDARY_NOT_IN_COMMUNITY


# ---------------------------------------------------------------------------
# The Digital Twin client
# ---------------------------------------------------------------------------

_client: Any | None = None


def _token_provider():
    """This service's own identity, ``svc-onboarding``.

    Its default token carries ``digital-twin.values.read``, which is what gives
    it the Digital Twin's audience (ADR-0011 in celine-policies, D37). Never the
    applicant's: the eligibility route is anonymous and the call is this
    service's (REQ-0006).
    """
    from celine.onboarding.services.service_auth import celine_token_provider

    return celine_token_provider()


def _get_client():
    """The Digital Twin client, built once, from celine-sdk."""
    global _client
    if _client is None:
        from celine.sdk.dt.client import DTClient

        _client = DTClient(
            base_url=settings.digital_twin_url.rstrip("/"),
            token_provider=_token_provider(),
            timeout=settings.digital_twin_timeout,
        )
    return _client


def reset_client() -> None:
    """Drop the cached client. For tests, and for a settings change at boot."""
    global _client
    _client = None


def _failure(exc: BaseException) -> str:
    """What went wrong, without anything the exception carries as content."""
    status = getattr(exc, "status_code", None)
    return f"{type(exc).__name__}" + (f" (status {int(status)})" if status is not None else "")


async def _fetch(community: str, fetcher: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not settings.digital_twin_url:
        logger.error(
            "DIGITAL_TWIN_URL is not set, so %s cannot be asked; a community whose "
            "template declares boundaries cannot check an address",
            fetcher,
        )
        raise DigitalTwinUnavailableError(f"{fetcher}: no Digital Twin is configured")

    try:
        result = await _get_client().communities.fetch_values(
            community or "default", fetcher, payload=dict(payload)
        )
        items = [item.to_dict() for item in result.items]
    except Exception as exc:  # every failure to answer is one failure
        logger.warning("The Digital Twin did not answer %s: %s", fetcher, _failure(exc))
        raise DigitalTwinUnavailableError(f"{fetcher}: {_failure(exc)}") from None
    return items


def _boundary_id(item: dict[str, Any], fetcher: str) -> str:
    value = item.get("id")
    if not isinstance(value, str) or not value:
        logger.warning("The Digital Twin answered %s with a row that has no id", fetcher)
        raise DigitalTwinUnavailableError(f"{fetcher}: an answer without an id")
    return value


async def boundary_at_point(source: str, lat: float, lon: float, *, community: str) -> str | None:
    """The id of the boundary covering the point, or ``None`` when none does.

    The Digital Twin counts a point on an edge as inside and resolves a point in
    two shapes to the lowest id (D28), so the answer is at most one id.
    """
    items = await _fetch(community, AT_POINT, {"source": source, "lat": lat, "lon": lon})
    if not items:
        return None
    return _boundary_id(items[0], AT_POINT)


async def known_boundary_ids(source: str, ids: list[str], *, community: str) -> set[str]:
    """The ids among *ids* that the Digital Twin answers a shape for.

    An unknown id is simply absent from its answer. Asked in batches of the
    fetcher's own limit.
    """
    wanted = sorted(set(ids))
    known: set[str] = set()
    for start in range(0, len(wanted), SHAPE_BATCH):
        batch = wanted[start : start + SHAPE_BATCH]
        for item in await _fetch(community, SHAPE, {"source": source, "ids": batch}):
            boundary_id = _boundary_id(item, SHAPE)
            if boundary_id in batch and item.get("geojson"):
                known.add(boundary_id)
    return known


# ---------------------------------------------------------------------------
# Template import
# ---------------------------------------------------------------------------


async def verify_template_boundaries(manifest: dict[str, Any], *, where: str) -> None:
    """Refuse a template naming a boundary the Digital Twin does not know (REQ-0002).

    Run by template import after the structural checks. A Digital Twin that
    cannot be asked refuses the template too, with that reason: a template is
    never imported unvalidated. A template without boundaries asks nothing.
    """
    block = manifest.get("rec_registry")
    if not template_service.declares_boundaries(block):
        return

    community = str(block.get("community") or "").strip()
    by_source: dict[str, list[str]] = {}
    for entry in block["areas"].values():
        ref = entry["boundary"]
        by_source.setdefault(ref["source"], []).append(ref["id"])

    unknown: list[str] = []
    for source, ids in sorted(by_source.items()):
        try:
            known = await known_boundary_ids(source, ids, community=community)
        except DigitalTwinUnavailableError as exc:
            raise BoundariesUncheckedError(
                f"{where}: the boundaries could not be checked, so the template is not "
                f"imported — the Digital Twin did not answer ({exc})"
            ) from None
        unknown.extend(f"{i} ({source})" for i in sorted(set(ids) - known))

    if unknown:
        raise ValueError(
            f"{where}: the Digital Twin knows no boundary {', '.join(unknown)}. "
            "Every area's boundary id must exist in its source."
        )


# ---------------------------------------------------------------------------
# Resolving a point or an address
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """Where a point is, as far as this community is concerned.

    ``boundary_id`` is the boundary covering the point, whether or not the
    template declares it; ``area`` is the template's area with that boundary, or
    ``None``. Eligible exactly when ``area`` is set.
    """

    source: str | None = None
    boundary_id: str | None = None
    area: str | None = None

    @property
    def eligible(self) -> bool:
        return self.area is not None


async def resolve_point(binding: RecRegistryBinding, lat: float, lon: float) -> Resolution:
    """Ask each of the template's boundary sources where the point falls.

    Raises :class:`DigitalTwinUnavailableError` when a source cannot be asked.
    """
    first = Resolution()
    for source in binding.boundary_sources:
        boundary_id = await boundary_at_point(source, lat, lon, community=binding.community)
        if boundary_id is None:
            continue
        area = binding.area_for_boundary(source, boundary_id)
        if area is not None:
            return Resolution(source=source, boundary_id=boundary_id, area=area)
        if first.boundary_id is None:
            first = Resolution(source=source, boundary_id=boundary_id)
    return first


async def resolve_address(binding: RecRegistryBinding, address: str) -> Resolution:
    """Geocode *address*, then :func:`resolve_point`.

    Raises ``ValueError`` (not quoting the address) when the geocoder finds no
    such address, and
    :class:`BoundaryUnavailableError` when the geocoder or the Digital Twin does
    not answer. The point exists only inside this call.
    """
    from celine.onboarding.services.eligibility import GeocoderUnavailableError, geocode_address

    try:
        addr = await geocode_address(address)
        return await resolve_point(binding, addr.lat, addr.lng)
    except ValueError:
        # The geocoder's own message quotes the address; this one does not.
        raise ValueError("the supply address could not be found") from None
    except GeocoderUnavailableError:
        raise BoundaryUnavailableError("the address service did not answer") from None
    except DigitalTwinUnavailableError as exc:
        raise BoundaryUnavailableError(f"the Digital Twin did not answer ({exc})") from None
