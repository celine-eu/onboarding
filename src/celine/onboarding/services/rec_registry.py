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
from typing import Any

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission
from celine.onboarding.services import template_service
from celine.onboarding.services.provisioning import participant_username as _username_from_email

logger = logging.getLogger(__name__)

_client: Any | None = None


def _get_client():
    """The registry client, built once.

    Authenticated with the same service token the rest of the integration uses.
    That works because every outbound call this app makes is issued by one realm
    (``celine``) — the registry validates against it just as the dataspace
    services do.

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

        from celine.onboarding.services.dataspace_identity import _get_token_provider

        _client = RecRegistryAdminClient(
            base_url=settings.rec_registry_url.rstrip("/"),
            token_provider=_get_token_provider(),
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
    Lavarone" would match Roma. A discrete field is either right or absent, and
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


def build_member_payload(
    submission: Submission,
    binding: template_service.RecRegistryBinding,
    *,
    keycloak_username: str | None = None,
) -> dict[str, Any]:
    """The member body, as the registry's own bundle schema expects it.

    ``key`` and ``user_id`` are different identifiers and deliberately so: the
    key is the registry's own handle on the member, the ``user_id`` is who they
    log in as. See :func:`member_user_id`.
    """
    extra = submission.extra_data or {}
    name = " ".join(part for part in (submission.first_name, submission.last_name) if part).strip()

    payload: dict[str, Any] = {
        "key": submission.ref,
        "user_id": member_user_id(submission, keycloak_username),
        "name": name or submission.ref,
        "type": "schema:Person",
        "role": member_role(submission),
        "area": binding.area_for(supply_municipality(submission)),
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
        payload["delivery_points"] = [
            {
                "id": submission.pod_code,
                "type": "pod",
                "description": "Supply point declared at onboarding",
                "active": True,
            }
        ]

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

    holder = await _get_client().lookup_member_by_user_id(user_id)
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

    payload = build_member_payload(submission, binding, keycloak_username=keycloak_username)

    from celine.sdk.openapi.rec_registry.models import MemberCreate

    response = await _get_client().create_member(binding.community, MemberCreate.from_dict(payload))

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

    response = await _get_client().delete_member(binding.community, member_key, purge=False)
    status = getattr(response, "status_code", None)
    status_value = int(status) if status is not None else 0

    if status_value >= 400 and status_value != 404:
        body = getattr(response, "content", b"")
        detail = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        raise ValueError(
            f"REC registry refused to deactivate member {member_key!r} in "
            f"community {binding.community!r} ({status_value}): {detail}"
        )

    logger.info("Deactivated member %s in community %s", member_key, binding.community)
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
    response = await _get_client().patch_member(binding.community, member_key, MemberPatch(did=did))

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
