"""The boundary a submission's supply address falls in, resolved by this service.

For a community whose template declares boundaries, a submission records
``supply_boundary_id`` and ``supply_boundary_source``. **The client never sends
them** (REQ-0007): no request schema declares either field, and the wizard's
copy of an eligibility answer is not evidence of where the supply point is. They
are resolved here, from the supply address the submission itself holds, at three
moments:

- **on save**, when the supply address changed (:func:`refresh_on_save`);
- **on submit** (:func:`resolve_for_submit`), which refuses an address outside
  every declared boundary;
- **at approval** (:func:`resolve_for_approval`), against the template in force
  then, which fails the registry step with ``boundary_not_in_community`` rather
  than falling back (REQ-0008).

The geocoded point is never stored and never logged; the id is all later steps
need. A resolution nobody could compute records nothing and never a guess.

**The supply address is the one the wizard checked** (REQ-0018): the eligibility
step saves the address it geocoded as ``supply_address`` (``{"text": ...}``,
encrypted), and that is what the server geocodes again. A submission without it
falls back to ``extracted_data["indirizzo"]``, the bill's supply address where
document scanning is on. A submission with neither has nothing to resolve.
Neither is ever logged.
"""

from __future__ import annotations

import logging

from celine.onboarding.models.submission import Submission
from celine.onboarding.services import boundaries, template_service
from celine.onboarding.services.boundaries import (
    BoundaryNotInCommunityError,
    BoundaryUnavailableError,
    Resolution,
)

logger = logging.getLogger(__name__)

SUPPLY_ADDRESS_KEY = "indirizzo"


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def supply_address(submission: Submission) -> str | None:
    """The supply address the submission holds, or ``None``.

    The address the wizard checked (``supply_address.text``) first; the scanned
    ``extracted_data["indirizzo"]`` only when there is none (REQ-0018).
    """
    checked = getattr(submission, "supply_address", None)
    if isinstance(checked, dict) and (text := _text(checked.get("text"))):
        return text
    extracted = getattr(submission, "extracted_data", None) or {}
    if not isinstance(extracted, dict):
        return None
    return _text(extracted.get(SUPPLY_ADDRESS_KEY))


def _binding(submission: Submission) -> template_service.RecRegistryBinding | None:
    binding = template_service.rec_registry_binding(submission.rec_slug)
    return binding if binding.uses_boundaries else None


def _record(submission: Submission, resolution: Resolution) -> None:
    submission.supply_boundary_id = resolution.boundary_id
    submission.supply_boundary_source = resolution.source if resolution.boundary_id else None


def _clear(submission: Submission) -> None:
    submission.supply_boundary_id = None
    submission.supply_boundary_source = None


async def refresh_on_save(submission: Submission, *, address_before: str | None) -> None:
    """Resolve again after a save that changed the supply address.

    Never fails the save. The changed address invalidates the recorded id first,
    so what stays recorded is always what the current address resolves to, or
    nothing. A save that leaves the address alone asks nothing: the wizard saves
    often, and each resolution is a geocoder and a Digital Twin call. The submit
    resolves again whatever the save recorded.
    """
    binding = _binding(submission)
    if binding is None:
        return

    address = supply_address(submission)
    if address == address_before:
        return
    _clear(submission)
    if address is None:
        return

    try:
        _record(submission, await boundaries.resolve_address(binding, address))
    except ValueError:
        logger.info("Submission %s: the supply address was not found", submission.ref)
    except BoundaryUnavailableError as exc:
        # Nothing written: an unanswered check is not a result (REQ-0005). The
        # submit resolves again and refuses while it cannot.
        logger.warning("Submission %s: boundary not resolved on save: %s", submission.ref, exc)


async def resolve_for_submit(submission: Submission) -> None:
    """Resolve and record the boundary, refusing a submit outside the community.

    Raises ``ValueError`` (a ``422``) when there is no supply address, it cannot
    be found, or it falls in no boundary of the template's areas; and
    :class:`BoundaryUnavailableError` (a ``503``) when it cannot be checked now,
    leaving the submission as it was.
    """
    binding = _binding(submission)
    if binding is None:
        return

    address = supply_address(submission)
    if address is None:
        raise ValueError(
            "Cannot submit: there is no supply address to check against the community's area"
        )
    try:
        resolution = await boundaries.resolve_address(binding, address)
    except ValueError as exc:
        raise ValueError(f"Cannot submit: {exc}") from None

    _record(submission, resolution)
    if not resolution.eligible:
        raise ValueError("Cannot submit: the supply address is not in the community's area")


async def resolve_for_approval(
    submission: Submission, binding: template_service.RecRegistryBinding
) -> str:
    """The area the approved member is registered into (REQ-0008).

    Resolved again from the submission's supply address against *binding*, the
    template in force now. The id it resolves is recorded on the submission, in
    or out of the template. Raises :class:`BoundaryNotInCommunityError` when no
    area of *binding* has that boundary — never falling back to a municipality,
    a default area or the id recorded earlier — and
    :class:`BoundaryUnavailableError` when it cannot be resolved now. Either
    fails the registry step, which a retry runs again.
    """
    address = supply_address(submission)
    if address is None:
        raise BoundaryNotInCommunityError(
            f"{boundaries.BOUNDARY_NOT_IN_COMMUNITY}: submission {submission.ref} holds no "
            "supply address, so no boundary can be resolved for it"
        )
    try:
        resolution = await boundaries.resolve_address(binding, address)
    except ValueError:
        raise BoundaryNotInCommunityError(
            f"{boundaries.BOUNDARY_NOT_IN_COMMUNITY}: the supply address of submission "
            f"{submission.ref} could not be found by the geocoder"
        ) from None

    _record(submission, resolution)
    if resolution.area is None:
        where = (
            f"primary substation {resolution.boundary_id} ({resolution.source})"
            if resolution.boundary_id
            else "no boundary at all"
        )
        raise BoundaryNotInCommunityError(
            f"{boundaries.BOUNDARY_NOT_IN_COMMUNITY}: the supply address of submission "
            f"{submission.ref} is in {where}, which is not the boundary of any area of "
            f"REC {submission.rec_slug!r}. Nothing was registered; retry once the "
            "template declares it"
        )
    return resolution.area


def area_of(submission: Submission) -> str | None:
    """The area the recorded boundary maps to in the template in force, for review."""
    boundary_id = getattr(submission, "supply_boundary_id", None)
    if not boundary_id:
        return None
    try:
        binding = template_service.rec_registry_binding(submission.rec_slug)
    except (KeyError, ValueError):
        return None
    source = getattr(submission, "supply_boundary_source", None)
    return binding.area_for_boundary(source, boundary_id)


def area_name_of(submission: Submission) -> str | None:
    """The display name of :func:`area_of`'s area, for review (REQ-0023).

    The template's ``name`` for that area, or its key when it gives none; ``None``
    when the template in force has no area on the recorded boundary.
    """
    area = area_of(submission)
    if area is None:
        return None
    return template_service.rec_registry_binding(submission.rec_slug).area_name(area)
