"""Register an approved participant as a member of the REC registry.

The third effect of approval. Enabling somebody means three things land:

1. a Keycloak user, so they can log in;
2. a **registry member**, so the rest of the platform can find them — this file;
3. a dataspace identity and their standing sharing consent.

Without (2) an approved participant is enabled in name only: invisible to every
pipeline, dashboard and digital-twin query, which all join on the registry's
``user_id``, POD and sensor ids. That is why this step **fails closed** while
share provisioning does not — a missing consent row is recoverable, a member who
does not exist is not a state anything downstream can work around.

Ordering matters and is not cosmetic. The registry keys a member on
``(community, user_id)``, so the Keycloak user has to exist first; the dataspace
identity comes last because it is the step that can be retried.

That ordering is why this module writes to the registry **twice**.
:func:`register_member` runs at (2); :func:`set_member_did` runs after (3),
because the DID it writes does not exist until the identity step mints it. The
second write is what lets the rest of the platform join a dataspace consent back
to the member who gave it.

It also **reads**, outside enablement entirely. :func:`supply_points_by_did` is
the other half of that join: the POD export asks the connector who consents, in
DIDs, and asks here what they hold. That is why the export no longer reads the
submission it was once written from — the form is what somebody typed once, and
the registry is what the community says now.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable
from types import SimpleNamespace
from typing import Any

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission
from celine.onboarding.services import template_service
from celine.onboarding.services.provisioning import participant_username as _username_from_email

logger = logging.getLogger(__name__)

_client: Any | None = None


def _get_client():
    """The registry client, built once.

    Authenticated as :func:`~celine.onboarding.services.service_auth.registry_token_provider`
    (REQ-0022): ``svc-onboarding`` when the dataspace is disabled, and the
    dataspace client ``svc-ds-onboarding`` only when ``DATASPACE_ENABLED``. The
    dataspace client is declared only on a realm that hosts the dataspace, so
    without it approval could not register anybody (D60). Every outbound call
    this app makes is issued by one realm (``celine``), and the registry
    validates against it.

    The client needs ``rec-registry.members.write`` to register a member and
    ``rec-registry.lookup`` to read supply points back — one grant for both
    lookup actions, which is why there is no ``rec-registry.assets.lookup`` to
    declare. Not ``assets.write``, since onboarding registers no assets, and
    certainly not ``rec-registry.admin``, which would also grant importing,
    exporting and purging. It also needs the registry in its audience.
    """
    global _client
    if _client is None:
        from celine.sdk.rec_registry.client import RecRegistryAdminClient

        from celine.onboarding.services.service_auth import registry_token_provider

        _client = RecRegistryAdminClient(
            base_url=settings.rec_registry_url.rstrip("/"),
            token_provider=registry_token_provider(),
        )
    return _client


def supply_municipality(submission: Submission) -> str | None:
    """The municipality of the supply address.

    Prefers the value the **eligibility geocoder** resolved: a geocoder returns
    the municipality as its own field, while a bill states a full address as
    free text and OCR of it is a guess. Falls back to the extraction's discrete
    ``comune`` when the wizard skipped the eligibility step.

    Neither source substring-matches the address, deliberately. Italian street
    names routinely contain other municipalities' names, so "Via Roma 1,
    Springfield" would match Roma. A discrete field is either right or absent, and
    absent resolves to the community's default area.
    """
    geocoded = getattr(submission, "supply_municipality", None)
    if isinstance(geocoded, str) and geocoded.strip():
        return geocoded.strip()

    extracted = submission.extracted_data or {}
    value = extracted.get("comune")
    return value.strip() if isinstance(value, str) and value.strip() else None


def member_role(submission: Submission) -> str:
    """`prosumer` when the participant declares generation, `consumer` otherwise.

    Read from the energy step's answers. A community that asks different
    questions gets `consumer`, which is the safe reading: claiming somebody
    produces when they do not would put them in the wrong settlement group.
    """
    extra = submission.extra_data or {}
    return "prosumer" if extra.get("has_pv") else "consumer"


def member_user_id(submission: Submission, keycloak_username: str | None = None) -> str:
    """What the registry resolves this person by: their **Keycloak username**.

    Not the member key, and not a UUID. Every self-service route in the registry
    matches ``Member.user_id`` against the token's ``preferred_username``, so a
    row holding anything else belongs to a member who can never see it — they get
    ``403 You are not a member of any community`` and the message points at the
    registry rather than at what wrote the row. The registry's own comment beside
    the column says "e.g. Keycloak UUID", which names the one value that does not
    work.

    ``keycloak_username`` is the value provisioning read back, and it wins. The
    normalised email is the fallback: it is what the provisioning service names
    an account it *creates*, so it is right for all of them and wrong only for
    an account adopted from another convention. ``submission.ref`` is the last
    resort — it is the broken value, kept only because there is nothing better
    when a submission has no email at all, and logged so it is not silent.
    """
    if keycloak_username and keycloak_username.strip():
        return keycloak_username.strip()

    email = _username_from_email(submission)
    if email:
        return email

    logger.warning(
        "Submission %s has no Keycloak username and no email; registering with its "
        "reference as user_id, which the registry cannot resolve a caller by",
        submission.ref,
    )
    return submission.ref


def member_name(submission: Submission) -> str:
    """The registry member's ``name``: first and last name, or the ref with neither.

    One function for approval and for a correction of either name
    (``services/propagation.py``), so a corrected member reads as a new one would.
    """
    name = " ".join(part for part in (submission.first_name, submission.last_name) if part)
    return name.strip() or submission.ref


def delivery_point_body(pod: str) -> dict[str, Any]:
    """A member's supply point as approval registers it, and a correction re-registers it."""
    return {
        "id": pod,
        "type": "pod",
        "description": "Supply point declared at onboarding",
        "active": True,
    }


def build_member_payload(
    submission: Submission,
    binding: template_service.RecRegistryBinding,
    *,
    keycloak_username: str | None = None,
    area: str | None = None,
) -> dict[str, Any]:
    """The member body, as the registry's own bundle schema expects it.

    ``key`` and ``user_id`` are different identifiers and deliberately so: the
    key is the registry's own handle on the member, the ``user_id`` is who they
    log in as. See :func:`member_user_id`.

    ``area`` is the area already resolved by boundary, and is required for a
    community whose areas are boundaries: its member's area is the boundary the
    supply address falls in and nothing else (ADR-0013), so there is no
    municipality or default to fall back to. For any other community it is
    decided here from the municipality lists.
    """
    if area is None:
        if binding.uses_boundaries:
            raise ValueError(
                f"Submission {submission.ref}: this community's areas are boundaries, "
                "and no area was resolved for the member"
            )
        area = binding.area_for(supply_municipality(submission))

    extra = submission.extra_data or {}

    payload: dict[str, Any] = {
        "key": submission.ref,
        "user_id": member_user_id(submission, keycloak_username),
        "name": member_name(submission),
        "type": "schema:Person",
        "role": member_role(submission),
        "area": area,
        "status": "active",
        "delivery_points": [],
        # No assets. What the wizard collects is **self-stated**: somebody
        # ticking "I have a photovoltaic system" is a declaration, not a
        # commissioned installation. Registering it as an asset would make an
        # unverified claim indistinguishable from a surveyed one, and a meter
        # cannot be registered at all — its `sensor_id` is assigned when the
        # device is physically installed, after onboarding. Asset registration
        # is the REC manager's offline work.
        "assets": {},
    }

    # The POD is the one thing that must be tracked from onboarding. It is what
    # the distributor keys on, what the metering data arrives against, and what
    # the supply-point export hands over — and unlike a meter it is known before
    # any device is installed.
    if submission.pod_code:
        payload["delivery_points"] = [delivery_point_body(submission.pod_code)]

    # The energy answers, kept as declarations rather than assets. They are what
    # a REC manager works from when deciding what to survey and commission, so
    # losing them between the wizard and the registry would mean asking again.
    declared = {
        key: extra[key]
        for key in ("has_pv", "pv_kwp", "has_battery", "battery_kwh", "has_ev", "has_heat_pump")
        if key in extra
    }
    if declared:
        payload["extra"] = {"declared_at_onboarding": declared}

    # How the REC verified this person and their POD before approving, for whoever
    # joins on the member — the DSO sharing their data among them. The same value
    # the dataspace credential carries as `verificationMethod`. Who recorded it
    # stays in onboarding's audit trail. A top-level key, because the registry
    # stores every key it has no column for directly in the member's `extra`.
    current = getattr(submission, "verification", None)
    if current is not None:
        payload["identity_verification"] = {
            "method": current.verification_method.credential_value,
            "verified_at": current.created_at.isoformat(),
        }

    return payload


async def _registry_response(call: Awaitable[Any]) -> Any:
    """The registry's answer, whatever its status.

    The SDK's admin client is built with ``raise_on_unexpected_status=True``, so a status
    the registry's OpenAPI does not declare — the ``409`` of a taken key, the ``404`` of a
    member already gone — arrives as ``UnexpectedStatus`` rather than as a response. Every
    caller here branches on the status and reads the body, so both shapes become one.
    Without this a retried registration failed on the very ``409`` it exists to accept
    (measured against rec-registry, celine-dev, 2026-09-15).
    """
    from celine.sdk.openapi.rec_registry.errors import UnexpectedStatus

    try:
        return await call
    except UnexpectedStatus as exc:
        return SimpleNamespace(status_code=exc.status_code, content=exc.content)


def _conflict_detail(response: Any) -> str:
    """The registry's own sentence for a ``409``: FastAPI's ``{"detail": ...}``."""
    body = getattr(response, "content", b"")
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    try:
        detail = json.loads(text).get("detail")
    except (ValueError, AttributeError):
        return text
    return detail if isinstance(detail, str) else text


async def _accept_conflict(community: str, *, key: str, user_id: str, detail: str) -> None:
    """Return when a ``409`` on create is this participant's member; raise otherwise.

    The registry answers ``409`` for several unique rules, and only one of them is
    the retry approval expects:

    - **the key is taken** — this submission's member, from an earlier attempt
      whose outcome was lost. Success.
    - **the ``user_id`` is taken** — somebody else in this community already logs
      in as this person. No member was created for this submission.
    - **the DID is taken**, or (planned) **a delivery point is already held** by
      another active member. Again, no member was created.

    Recording success for any but the first leaves the participant approved and
    absent from the registry — invisible to every pipeline, dashboard and twin
    query, with nothing marking it — and hands step 3 a member key that is not
    theirs. So this branches on the registry's reason rather than the status, and
    a wording it does not recognise fails closed.

    **A key retry is confirmed through the ``user_id`` lookup**, not by reading
    the member by key: that read needs ``rec-registry.read``, which this client is
    deliberately not granted, while the lookup is covered by the
    ``rec-registry.lookup`` it already holds. The registry keeps ``user_id``
    unique per community, so this ``user_id`` on a *different* member here means
    the key row is not this person's. Finding nobody, or somebody in another
    community (the lookup returns the first row across communities), does not
    contradict the retry: a retry of step 2 alone sends the email fallback rather
    than the username step 1 read back. The key is ``submission.ref``, which only
    this service writes, so that stays a success — logged, because it was not
    confirmed.
    """
    key_taken = detail.startswith(f"Member {key!r}") and "already exists" in detail
    if not key_taken:
        raise ValueError(
            f"REC registry refused member {key!r} for community {community!r} (409): "
            f"{detail}. No member was registered for this submission; an operator has "
            "to resolve the conflict in the registry before this step is retried"
        )

    from celine.sdk.openapi.rec_registry.errors import UnexpectedStatus

    try:
        holder = await _get_client().lookup_member_by_user_id(user_id)
    except UnexpectedStatus as exc:
        # The registry's lookups answer from **active** members only
        # (rec-registry REQ-0097..0099).
        if exc.status_code == 404:
            # No active member logs in as this person: nothing contradicts the
            # retry, and it reads as the lookup finding nobody.
            holder = None
        elif exc.status_code == 409:
            raise ValueError(
                f"REC registry holds user_id {user_id!r} as an active member of more than "
                f"one community (409 ambiguous_member), so member {key!r} in "
                f"{community!r} cannot be confirmed as this participant's. An operator "
                "has to release the person from the other community before this step is "
                "retried"
            ) from exc
        else:
            raise
    if holder is not None and holder.community_key == community and holder.key != key:
        raise ValueError(
            f"REC registry already holds member {key!r} in community {community!r}, but "
            f"user_id {user_id!r} belongs to member {holder.key!r} there. Member {key!r} "
            "is not this participant's; an operator has to resolve which is"
        )

    if holder is not None and holder.community_key == community:
        logger.info("Member %s in %s was already registered as %r", key, community, user_id)
    else:
        logger.warning(
            "Member %s already exists in %s, and user_id %r is not on it as far as the "
            "lookup can tell; accepting it as this submission's earlier registration",
            key,
            community,
            user_id,
        )


async def register_member(
    submission: Submission, *, keycloak_username: str | None = None
) -> str | None:
    """Register the approved participant, returning the member key.

    ``keycloak_username`` is what provisioning read back from Keycloak, and it
    becomes the member's ``user_id`` — the thing that lets them resolve
    themselves. Omitting it falls back to the submission's email; see
    :func:`member_user_id` for when that differs.

    Returns ``None`` when registration is not configured — a deployment with no
    registry, or a community whose manifest declares no ``rec_registry`` block.
    Both are supported configurations rather than degraded ones.

    Raises on failure, so approval does not complete. An already-registered
    participant is **not** a failure: this runs on approval, approval can be
    retried, and refusing the second attempt would leave a submission that can
    never be approved. But a ``409`` is not always that participant — see
    :func:`_accept_conflict`.
    """
    if not settings.rec_registry_url:
        return None

    await template_service.ensure_fresh()
    binding = template_service.rec_registry_binding(submission.rec_slug)
    if not binding.enabled:
        logger.debug(
            "REC %r declares no rec_registry binding; skipping registration",
            submission.rec_slug,
        )
        return None

    area: str | None = None
    if binding.uses_boundaries:
        # Resolved again, from the supply address, against the template in force
        # now (REQ-0008). Raises `BoundaryNotInCommunityError` for a boundary no
        # area declares any more, and `BoundaryUnavailableError` when it cannot
        # be resolved: either fails this step before anything is written, and a
        # retry resolves again.
        from celine.onboarding.services.supply_boundary import resolve_for_approval

        area = await resolve_for_approval(submission, binding)

    payload = build_member_payload(
        submission, binding, keycloak_username=keycloak_username, area=area
    )

    from celine.sdk.openapi.rec_registry.models import MemberCreate

    response = await _registry_response(
        _get_client().create_member(binding.community, MemberCreate.from_dict(payload))
    )

    status = getattr(response, "status_code", None)
    status_value = int(status) if status is not None else 0

    if status_value == 409:
        await _accept_conflict(
            binding.community,
            key=payload["key"],
            user_id=payload["user_id"],
            detail=_conflict_detail(response),
        )
        return payload["key"]

    if status_value >= 400:
        body = getattr(response, "content", b"")
        detail = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        raise ValueError(
            f"REC registry refused member {payload['key']!r} for community "
            f"{binding.community!r} ({status_value}): {detail}"
        )

    logger.info(
        "Registered %s in community %s as %s",
        payload["key"],
        binding.community,
        payload["role"],
    )
    return payload["key"]


async def deactivate_member(submission: Submission, *, member_key: str) -> str:
    """Deactivate a community member — reversing registration, not erasing it.

    `delete_member(purge=False)` is the registry's deactivation. Erasure is a
    separate, irreversible act with its own scope (`rec-registry.members.purge`),
    and reversing an approval is not the same decision as answering an erasure
    request.

    A `404` counts as done: the member is not there either way, and refusing
    would leave the local record claiming something that is no longer true.
    """
    if not settings.rec_registry_url:
        return "no registry configured"

    await template_service.ensure_fresh()
    binding = template_service.rec_registry_binding(submission.rec_slug)
    if not binding.enabled:
        return "this community declares no rec_registry binding"

    return await deactivate_registry_member(binding.community, member_key)


async def deactivate_registry_member(community: str, member_key: str) -> str:
    """:func:`deactivate_member` for a caller holding the registry's own pair.

    A member imported into the registry has no submission, so the release keys on
    ``(community, member_key)``. Same reading: ``purge=False``, a ``404`` is done.
    """
    response = await _registry_response(
        _get_client().delete_member(community, member_key, purge=False)
    )
    status = getattr(response, "status_code", None)
    status_value = int(status) if status is not None else 0

    if status_value >= 400 and status_value != 404:
        body = getattr(response, "content", b"")
        detail = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        raise ValueError(
            f"REC registry refused to deactivate member {member_key!r} in "
            f"community {community!r} ({status_value}): {detail}"
        )

    logger.info("Deactivated member %s in community %s", member_key, community)
    return f"deactivated registry member {member_key}"


async def set_member_did(rec_slug: str, *, member_key: str, did: str) -> str:
    """Write the participant's dataspace DID onto their registry member.

    Takes a ``rec_slug`` rather than the ``Submission`` it used to, because the
    member's wizard mints DIDs for people who have no submission and never will.
    The community is the only thing the row was ever read for.

    **A second call, not a field on the create.** The DID does not exist when the
    member is registered: it is minted one step later, by the identity registry,
    so it can only arrive as an update to a row that already exists. Reordering
    the steps to have it earlier was considered and rejected — the dataspace step
    is last precisely because it is the one that can be retried, and moving it in
    front of registration would make member registration fail closed on a
    dataspace outage.

    **Why the registry needs it at all.** The connector answers *who consents* in
    DIDs, and the registry knows *what they hold*; nothing joined the two. This
    column is that join, and without it the POD export has no way to read consent
    from the running system — it is left reading the intake form, which is the
    defect this write exists to make fixable.

    Resolving the DID through the identity registry instead does not work and is
    not a longer version of the same thing: ``Member.user_id`` holds a Keycloak
    *username*, so the Keycloak user id that hop returns matches no row.

    **A clash raises.** The registry keys ``did`` globally unique, so a ``409``
    means another member already holds this DID — two people with one dataspace
    identity, which would attribute one person's supply points to the other in
    every export that follows. It is not a retry artefact and retrying will not
    clear it, so it fails the step rather than being logged past. Re-sending a
    member the DID it already holds is a no-op success at the registry, which is
    what keeps the retry of this step idempotent.
    """
    if not settings.rec_registry_url:
        return "no registry configured"

    await template_service.ensure_fresh()
    binding = template_service.rec_registry_binding(rec_slug)
    if not binding.enabled:
        return "this community declares no rec_registry binding"

    from celine.sdk.openapi.rec_registry.models import MemberPatch

    # Only `did` is sent. Absent fields are left alone by the registry, so this
    # cannot clobber anything an operator changed there since registration.
    response = await _registry_response(
        _get_client().patch_member(binding.community, member_key, MemberPatch(did=did))
    )

    status = getattr(response, "status_code", None)
    status_value = int(status) if status is not None else 0

    if status_value >= 400:
        body = getattr(response, "content", b"")
        detail = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        raise ValueError(
            f"REC registry refused the dataspace DID for member {member_key!r} in "
            f"community {binding.community!r} ({status_value}): {detail}"
        )

    logger.info(
        "Member %s in community %s now holds dataspace DID %s",
        member_key,
        binding.community,
        did,
    )
    return f"registry member {member_key} holds the dataspace DID"


async def set_member_name(rec_slug: str, *, member_key: str, name: str) -> str:
    """Write a corrected name onto the registry member.

    The general member ``PATCH`` with ``name`` alone; absent fields are left
    alone, as for :func:`set_member_did`. Re-sending the name the member already
    has is a no-op success, so a retry is safe.

    Raises ``RegistryRefusalError`` with the status only: the registry's body may
    quote the name, and the caller records the error on a step row that holds no
    values.
    """
    if not settings.rec_registry_url:
        return "no registry configured"

    await template_service.ensure_fresh()
    binding = template_service.rec_registry_binding(rec_slug)
    if not binding.enabled:
        return "this community declares no rec_registry binding"

    from celine.sdk.openapi.rec_registry.models import MemberPatch

    response = await _registry_response(
        _get_client().patch_member(binding.community, member_key, MemberPatch(name=name))
    )
    status = getattr(response, "status_code", None)
    status_value = int(status) if status is not None else 0
    if status_value >= 400:
        logger.warning(
            "REC registry refused the name of member %s in community %s (%s)",
            member_key,
            binding.community,
            status_value,
        )
        raise RegistryRefusalError(
            f"REC registry refused the name for member {member_key!r} in community "
            f"{binding.community!r} ({status_value})",
            status_code=status_value,
        )

    logger.info("Member %s in community %s has the corrected name", member_key, binding.community)
    return f"registry member {member_key} holds the corrected name"


async def replace_delivery_point(
    rec_slug: str, *, member_key: str, pod: str, replaces: str | None
) -> str:
    """Put a corrected POD on the registry member, replacing the one it held.

    One write (registry 1.7.0+, R15): with ``replaces`` the registry adds ``pod``,
    removes ``replaces`` and relinks the member's meters whose ``pod`` named it,
    in one transaction; a failure changes nothing. Without ``replaces`` — the
    member held no POD — it only adds. The body is approval's
    (:func:`delivery_point_body`).

    Raises ``RecRegistryApiError`` unchanged: the caller branches on ``code``
    (``delivery_point_held``) and on a ``404`` with no code (``replaces`` is not
    this member's). Its text may name another member or the POD, so the caller
    records the code, never the text.
    """
    if not settings.rec_registry_url:
        return "no registry configured"

    await template_service.ensure_fresh()
    binding = template_service.rec_registry_binding(rec_slug)
    if not binding.enabled:
        return "this community declares no rec_registry binding"

    await _get_client().put_delivery_point(
        binding.community,
        member_key,
        pod,
        delivery_point_body(pod),
        replaces=replaces,
    )
    logger.info(
        "Member %s in community %s holds the corrected supply point%s",
        member_key,
        binding.community,
        " (replacing the previous one)" if replaces else "",
    )
    return f"registry member {member_key} holds the corrected supply point"


class RegistryUnavailableError(RuntimeError):
    """The registry could not be read: an outage, not an answer."""


async def registry_member(community: str, member_key: str) -> Any | None:
    """The member row ``(community, member_key)``, whatever its status, or ``None``.

    ``rec-registry.read`` (``GET /admin/communities/{c}/members/{k}``). ``None``
    for the registry's ``404``; any other failure raises
    :class:`RegistryUnavailableError`, because "nobody there" and "could not ask"
    must not read alike. The row carries ``user_id`` (the Keycloak username),
    ``did`` and ``status``.
    """
    try:
        response = await _registry_response(_get_client().get_member(community, member_key))
    except Exception as exc:  # noqa: BLE001 — transport or token: the registry cannot say
        raise RegistryUnavailableError(f"{type(exc).__name__}: {exc}") from exc
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 404:
        return None
    parsed = getattr(response, "parsed", None)
    if status != 200 or parsed is None:
        raise RegistryUnavailableError(
            f"REC registry answered {status} reading member {member_key!r} of {community!r}"
        )
    return parsed


class RegistryNotConfiguredError(LookupError):
    """There is no registry to ask for this REC: no URL, or no ``rec_registry`` block."""


async def shared_delivery_points(rec_slug: str) -> Any:
    """This community's delivery points that more than one active member holds.

    The registry's per-community report (registry plan F8, ``GET /admin/communities/
    {c}/delivery-points/duplicates``, ``rec-registry.read``): each point in its
    compared form (trimmed, lower-cased), this community's active holders by
    member key with the spelling each stored, and how many active members of
    other communities hold it, as a count only. These are the points the
    registry refuses to give again (``delivery_point_held``) until resolved.

    Returns the SDK's ``DeliveryPointDuplicatesSchema``. Raises
    :class:`RegistryNotConfiguredError` when there is no registry to ask, and
    lets ``RecRegistryApiError`` and transport errors through for the caller to
    answer.
    """
    if not settings.rec_registry_url:
        raise RegistryNotConfiguredError("no REC registry is configured")

    await template_service.ensure_fresh()
    binding = template_service.rec_registry_binding(rec_slug)
    if not binding.enabled:
        raise RegistryNotConfiguredError("this community declares no rec_registry binding")

    return await _get_client().list_duplicate_delivery_points(binding.community)


class RegistryRefusalError(ValueError):
    """The registry answered an error status; ``status_code`` is it."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


async def ensure_member_did(rec_slug: str, *, user_id: str, did: str) -> str:
    """Make sure this member's registry row carries *did*, and say what happened.

    **Why this exists.** The POD export joins the connector's answer to *who
    consented* — stated in DIDs — to the registry's answer to *what they hold*,
    through ``Member.did``. The approval path writes that column as part of
    enablement. The member's wizard mints DIDs outside enablement entirely, so
    without this a member provisioned there would consent, appear in the
    connector's audience, and contribute **no supply points to any export** —
    silently, because a missing join is indistinguishable from a person who holds
    nothing.

    Idempotent and safe to call on every read, which is the point: it also heals
    a row whose write failed once, and a member provisioned before this existed.
    A row that already holds the DID costs one lookup and no write.

    **A row holding a *different* DID is refused, not corrected.** That is one
    person with two dataspace identities, and picking one would silently move
    which consent record their supply points answer to. An operator has to decide
    which is real, so it is logged at error and left alone.

    Never raises: this runs beside a member reading their own consent page, and
    failing that page over a registry write would take away the thing they came
    for. Every outcome is a sentence, and the bad ones are logged.
    """
    if not settings.rec_registry_url:
        return "no registry configured"

    await template_service.ensure_fresh()
    binding = template_service.rec_registry_binding(rec_slug)
    if not binding.enabled:
        return "this community declares no rec_registry binding"

    try:
        member = await _get_client().lookup_member_by_user_id(user_id)
    except Exception:
        logger.exception(
            "Could not look up registry member %r in community %s; the dataspace "
            "DID %s is not on their row and their supply points will not be exported",
            user_id,
            binding.community,
            did,
        )
        return "registry lookup failed"

    if member is None:
        # They hold a dataspace identity and no membership in the community that
        # would disclose anything about them. Not an error here — but it is the
        # reason an export will not carry them, so it must not be silent.
        logger.warning(
            "No registry member with user_id %r in community %s; the dataspace "
            "DID %s has nowhere to go and this member's supply points cannot be exported",
            user_id,
            binding.community,
            did,
        )
        return "no registry member for this user"

    if member.community_key != binding.community:
        logger.error(
            "Registry member %s for user_id %r belongs to community %s, not %s; "
            "not writing the dataspace DID",
            member.key,
            user_id,
            member.community_key,
            binding.community,
        )
        return "registry member belongs to another community"

    existing = (getattr(member, "did", None) or "").strip()
    if existing == did:
        return "registry member already holds the dataspace DID"
    if existing:
        logger.error(
            "Registry member %s already holds dataspace DID %s, not %s. One person "
            "with two dataspace identities — an operator must decide which is real; "
            "not overwriting",
            member.key,
            existing,
            did,
        )
        return "registry member holds a different dataspace DID"

    try:
        return await set_member_did(rec_slug, member_key=member.key, did=did)
    except Exception:
        # Includes the registry's 409: another member already holds this DID.
        # Fatal on the approval path, where it fails the step; here it must not
        # take down the page, and the log is what an operator acts on.
        logger.exception(
            "Could not write dataspace DID %s onto registry member %s; their supply "
            "points will not be exported until it is",
            did,
            member.key,
        )
        return "registry refused the dataspace DID"


async def supply_points_by_did(dids: list[str], *, rec_slug: str) -> dict[str, list[str]] | None:
    """What the registry says each of these dataspace DIDs holds, as supply points.

    The second half of the join :func:`set_member_did` wrote. The connector
    answers *who consents*, in DIDs; this answers *what they hold*, and the POD
    export is the two put together — so it reads the running system twice and
    the intake form not at all.

    Returns ``None`` when there is no registry to ask: no ``REC_REGISTRY_URL``,
    or a community whose manifest declares no ``rec_registry`` block. Both are
    supported configurations, and the caller falls back to what it collected at
    intake rather than exporting nothing. ``{}`` is a different answer — the
    registry was asked and matched nobody — and must not be confused with it.

    **Only this community's members.** ``did`` is globally unique in the
    registry and the lookup is deliberately cross-community, so a DID can come
    back on a member of somewhere else. Their consent to the offer is real and
    their supply point is still not this community's to disclose, so the row is
    dropped here rather than at the registry.

    **Only active members.** ``pending``, ``suspended`` and ``inactive`` are all
    states in which the REC has said this person is not participating, and a
    disclosure of their supply point would contradict that. It also closes the
    gap left by revocation: ``delete_member`` deactivates the member but the DID
    stays on the row — nothing can unset it through ``PATCH`` — so status is the
    only thing that takes a revoked participant back out of the export.

    **Declared and commissioned supply points are unioned, not ranked.**
    ``Member.delivery_points`` carries the POD this service wrote at onboarding,
    before any meter exists; a commissioned meter carries its own in
    ``properties.pod`` and is reachable only through ``assets-by-user-ids`` and
    the ``user_id`` in the member row. They are two records of one fact and
    normally agree — but a member the REC manager imported may have either one
    alone, and dropping a supply point because the community recorded it in the
    other place would exclude somebody who consented.

    Requires ``rec-registry.lookup``, which grants both lookup actions. A refused
    request raises rather than answering an empty list: an empty list here means
    "these people hold nothing", and a denial that read as one would quietly
    export fewer supply points than were authorised.
    """
    if not settings.rec_registry_url:
        return None

    await template_service.ensure_fresh()
    binding = template_service.rec_registry_binding(rec_slug)
    if not binding.enabled:
        logger.debug("REC %r declares no rec_registry binding; not reading supply points", rec_slug)
        return None

    wanted = [did.strip() for did in dict.fromkeys(dids) if did and did.strip()]
    if not wanted:
        return {}

    client = _get_client()
    members = await client.lookup_members_by_dids(wanted)

    found: dict[str, list[str]] = {}
    did_by_user_id: dict[str, str] = {}

    for member in members:
        did = (getattr(member, "did", None) or "").strip()
        if not did:
            continue
        if member.community_key != binding.community:
            logger.info(
                "DID %s belongs to member %s of community %s, not %s; not in this export",
                did,
                member.key,
                member.community_key,
                binding.community,
            )
            continue
        if (member.status or "").strip().lower() != "active":
            logger.info(
                "Member %s in community %s is %s, so their supply points are not disclosed",
                member.key,
                binding.community,
                member.status,
            )
            continue

        pods = [
            dp.id.strip()
            for dp in (member.delivery_points or [])
            if dp.id and dp.id.strip() and dp.active is not False
        ]
        found[did] = list(dict.fromkeys(pods))
        if member.user_id:
            did_by_user_id.setdefault(member.user_id, did)

    if did_by_user_id:
        for asset in await client.lookup_assets_by_user_ids(list(did_by_user_id)):
            if asset.asset_type != "meter" or asset.community_key != binding.community:
                continue
            pod = str((asset.properties or {}).get("pod") or "").strip()
            did = did_by_user_id.get(asset.owner_user_id, "")
            if pod and did and pod not in found.get(did, []):
                found[did].append(pod)

    return found
