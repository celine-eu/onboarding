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
"""

from __future__ import annotations

import logging
from typing import Any

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission
from celine.onboarding.services import template_service

logger = logging.getLogger(__name__)

_client: Any | None = None


def _get_client():
    """The registry client, built once.

    Authenticated with the same service token the rest of the integration uses.
    That works because every outbound call this app makes is issued by one realm
    (``celine``) — the registry validates against it just as the dataspace
    services do.

    The client needs ``rec-registry.members.write`` and nothing else: not
    ``assets.write``, since onboarding registers no assets, and certainly not
    ``rec-registry.admin``, which would also grant importing, exporting and
    purging. It also needs the registry in its audience.
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


def build_member_payload(
    submission: Submission, binding: template_service.RecRegistryBinding
) -> dict[str, Any]:
    """The member body, as the registry's own bundle schema expects it."""
    extra = submission.extra_data or {}
    name = " ".join(
        part for part in (submission.first_name, submission.last_name) if part
    ).strip()

    payload: dict[str, Any] = {
        "key": submission.ref,
        "user_id": submission.ref,
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

    return payload


async def register_member(
    submission: Submission, *, keycloak_user_id: str | None = None
) -> str | None:
    """Register the approved participant, returning the member key.

    Returns ``None`` when registration is not configured — a deployment with no
    registry, or a community whose manifest declares no ``rec_registry`` block.
    Both are supported configurations rather than degraded ones.

    Raises on failure, so approval does not complete. An already-registered
    participant (``409``) is **not** a failure: this runs on approval, approval
    can be retried, and refusing the second attempt would leave a submission
    that can never be approved.
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

    payload = build_member_payload(submission, binding)

    from celine.sdk.openapi.rec_registry.models import MemberCreate

    response = await _get_client().create_member(
        binding.community, MemberCreate.from_dict(payload)
    )

    status = getattr(response, "status_code", None)
    status_value = int(status) if status is not None else 0

    if status_value == 409:
        # Already there. Approval is retriable, so treat this as success rather
        # than wedging a submission that cannot be approved a second time.
        logger.info(
            "Participant %s is already a member of %s",
            submission.ref,
            binding.community,
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
