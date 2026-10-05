"""Releasing a member from their REC, keyed on the registry's pair.

A REC admin releases a member on the community dashboard, and the call arrives
here by delegation (`api/admin/members.py`). Releasing is what lets the person
join another community (requester, 2026-10-05): **a person cannot be a member of
two RECs**, so until the first one releases them the second one is refused, by
the identity registry (another organisation's same-role credential, `409`) and
by the provisioning service (`409 member_of_another_community`).

## Four steps, keyed on the member rather than on a submission

The submission-keyed revocation (`enablement.revoke`) needs a submission, and a
member imported into the registry has none. So this runs the same four acts
itself, each **on its end state rather than on what a step row says**, in the
revocation's order:

1. ``dataspace_share``: at every connector the manifest names, each standing
   grant this community collected is withdrawn as the community's decision;
2. ``dataspace_identity`` (waits for 1): at the identity registry, the
   membership, then the credential (its status-list bit; the row is kept);
3. ``keycloak_user``: through provisioning, the login is disabled, taken out of
   the REC's organization and its groups, and every session is ended;
4. ``rec_registry_member`` (waits for 3): the member is set ``inactive``, never
   purged.

A step waits for another when undoing it first would make the other
unrepairable: the withdrawal needs the membership the identity step deletes, and
the release of the login resolves the member through the registry. A step whose
predecessor failed is `blocked` and left as it is; calling again is the retry,
and every step is idempotent.

**Where a submission exists** it is the source of the DID and the credential id,
its identity columns are cleared by the identity step, and the outcome of each
step is written on its enablement row as `enablement.revoke` would — so the
console tells the same story. Without one, the DID is the registry member's and
the credential is found through the identity registry, by the member's username.

**Nothing is deleted** (retention): consents are withdrawn, the credential is
revoked, the membership row of the registry is set inactive, the Keycloak
account is disabled and moved out. The DID is not retired and its Keycloak
mapping is not removed — the next community mints a new one (requester,
2026-10-05) and the identity registry rebinds the login then.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import jwt as pyjwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.config.settings import settings
from celine.onboarding.models.enablement import EnablementStatus, EnablementStep
from celine.onboarding.models.submission import Submission
from celine.onboarding.services import (
    dataspace_identity,
    enablement,
    provisioning,
    rec_registry,
    template_service,
)

logger = logging.getLogger(__name__)

#: The four steps, in the order they run and are answered.
STEPS: tuple[EnablementStep, ...] = (
    EnablementStep.DATASPACE_SHARE,
    EnablementStep.DATASPACE_IDENTITY,
    EnablementStep.KEYCLOAK_USER,
    EnablementStep.REC_REGISTRY_MEMBER,
)

#: A step that is not attempted while the one it names failed in this release.
WAITS_FOR: dict[EnablementStep, EnablementStep] = {
    EnablementStep.DATASPACE_IDENTITY: EnablementStep.DATASPACE_SHARE,
    EnablementStep.REC_REGISTRY_MEMBER: EnablementStep.KEYCLOAK_USER,
}

#: The code a step answers when it failed.
FAILED_CODES: dict[EnablementStep, str] = {
    EnablementStep.DATASPACE_SHARE: "withdrawal_failed",
    EnablementStep.DATASPACE_IDENTITY: "revocation_failed",
    EnablementStep.KEYCLOAK_USER: "release_failed",
    EnablementStep.REC_REGISTRY_MEMBER: "deactivation_failed",
}

DONE, SKIPPED, FAILED, BLOCKED = "done", "skipped", "failed", "blocked"


class MemberNotFoundError(LookupError):
    """The registry has no member under this key in this community."""


@dataclass
class StepResult:
    step: str
    status: str
    code: str
    detail: str


@dataclass
class Release:
    community: str
    member_key: str
    #: `submission` when an onboarding submission backs the member, else `registry`.
    source: str
    steps: list[StepResult] = field(default_factory=list)

    @property
    def state(self) -> str:
        """`released` when every step's end state holds, else `partial`."""
        if any(s.status in (FAILED, BLOCKED) for s in self.steps):
            return "partial"
        return "released"


@dataclass
class _Subject:
    """The part of a `Submission` the share and identity steps read and clear.

    For a member with no submission: the registry's DID, and the credential the
    identity registry says they hold for this community.
    """

    rec_slug: str
    ref: str
    dataspace_did: str | None
    dataspace_vc_id: str | None = None
    dataspace_vc_issued_at: Any = None
    share_provisioned: bool = False


def _value(obj: Any, name: str) -> Any:
    """A generated model's attribute: `None` for UNSET, an enum read as its value."""
    value = getattr(obj, name, None)
    if value is None or type(value).__name__ == "Unset":
        return None
    return getattr(value, "value", value)


async def _submission_for(db: AsyncSession, rec_slug: str, member_key: str) -> Submission | None:
    """The submission the member was registered from: the registry key is its `ref`."""
    result = await db.execute(
        select(Submission).where(Submission.rec_slug == rec_slug, Submission.ref == member_key)
    )
    return result.scalars().first()


# ---------------------------------------------------------------------------
# The identity a member with no submission holds
# ---------------------------------------------------------------------------


def _credential_payload(vc_jws: str) -> dict[str, Any]:
    """The JWT's claims, **unverified**: only to name the credential to revoke.

    The token comes from the identity registry over an authenticated call, and
    nothing here trusts it beyond choosing which credential id to ask that same
    registry to revoke. The registry signs `jti` = the credential's id.
    """
    try:
        return pyjwt.decode(vc_jws, options={"verify_signature": False})
    except pyjwt.PyJWTError:
        return {}


@dataclass
class HeldCredentials:
    """The active credentials on one DID, split by whose they are to revoke."""

    #: Credential ids linked to this community: the release revokes every one,
    #: whatever its role (ds ADR-0028: the login moves to the next REC's DID only
    #: once the old one holds no active credential).
    ours: list[str] = field(default_factory=list)
    #: How many another organisation linked. Not this community's to revoke, and
    #: while one stands the next REC's login sync is refused.
    elsewhere: int = 0


async def held_credentials(identifiers: dict[str, str], did: str, binding) -> HeldCredentials:
    """What ``did`` holds, found through the person's login (``POST /users/resolve``).

    ``identifiers`` is ``{"username": …}`` or ``{"email": …}``. The answer lists
    active, unexpired credentials of every role; each is the VC-JWT whose ``jti``
    is the credential id. A login that now resolves to another DID — the person
    joined another community since — yields nothing, and nothing of theirs is
    touched.
    """
    held = HeldCredentials()
    if not identifiers or not settings.identity_registry_url:
        return held
    access = await dataspace_identity.registry_access()
    body = await dataspace_identity._resolve_raw_with_params(access, identifiers)
    if not body or (body.get("did") or "") != did:
        return held
    for entry in body.get("credentials") or []:
        claims = _credential_payload(entry.get("vc_jws") or "")
        if claims.get("sub") != did or not claims.get("jti"):
            continue
        subject = ((claims.get("vc") or {}).get("credentialSubject")) or {}
        linked = subject.get("linkedParticipant")
        if binding.linked_participant_did and linked != binding.linked_participant_did:
            held.elsewhere += 1
        else:
            held.ours.append(str(claims["jti"]))
    return held


async def revoke_credential(credential_id: str, binding) -> None:
    """``DELETE /admin/credentials/{id}`` as the community's collector. A 404 is done."""
    import httpx

    headers = await dataspace_identity._collector_headers(
        binding.organization, dataspace_identity.CREDENTIALS_WRITE
    )
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(
            f"{settings.identity_registry_url.rstrip('/')}/admin/credentials/{credential_id}",
            headers=headers,
        )
    if resp.status_code >= 400 and resp.status_code != 404:
        raise ValueError(f"Credential revocation failed ({resp.status_code}): {resp.text}")


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------


async def _share(subject: _Subject | Submission) -> StepResult:
    step = EnablementStep.DATASPACE_SHARE
    if not settings.ds_connector_url:
        return StepResult(step, SKIPPED, "no_connector", "no dataspace connector is configured")
    if not subject.dataspace_did:
        return StepResult(
            step, SKIPPED, "no_dataspace_identity", "the member holds no dataspace identity"
        )
    written: list[str] = []
    await dataspace_identity.withdraw_user_shares(subject, raise_on_error=True, report=written)
    if written:
        return StepResult(step, DONE, "withdrawn", "; ".join(written)[:4000])
    return StepResult(
        step, DONE, "nothing_standing", "no grant this community collected stood at any connector"
    )


async def _identity(subject: _Subject | Submission, *, username: str | None) -> StepResult:
    step = EnablementStep.DATASPACE_IDENTITY
    did = subject.dataspace_did
    if not did:
        return StepResult(
            step, SKIPPED, "no_dataspace_identity", "the member holds no dataspace identity"
        )

    # Without an identity registry there is nothing to look up; the recorded
    # credential's revocation then says so itself.
    registry = bool(settings.identity_registry_url)
    binding = None
    held = HeldCredentials()
    email = getattr(subject, "email", None)
    identifiers = {"username": username} if username else ({"email": email} if email else {})
    if registry:
        await template_service.ensure_fresh()
        binding = template_service.dataspace_binding(subject.rec_slug)
        held = await held_credentials(identifiers, did, binding)
    if not subject.dataspace_vc_id and held.ours:
        subject.dataspace_vc_id = held.ours[0]

    revoked: list[str] = []
    detail = None
    if subject.dataspace_vc_id:
        revoked.append(subject.dataspace_vc_id)
        # The membership, then the recorded credential; clears the submission's
        # identity columns.
        detail = await dataspace_identity.revoke_user_identity(subject)
    elif binding is not None and binding.organization:
        # No credential to name: the membership is removed all the same — a DID
        # still a member of the organisation counts as one at every connector.
        # A 404 is logged as a misalignment and is the end state.
        await dataspace_identity._delete_membership(
            settings.identity_registry_url.rstrip("/"),
            await dataspace_identity._collector_headers(
                binding.organization, dataspace_identity.MEMBERSHIPS_WRITE
            ),
            did,
            binding.organization,
            subject_ref=subject.ref,
        )
    # Every other credential this community linked to the DID, whatever its role.
    for credential_id in held.ours:
        if credential_id not in revoked:
            await revoke_credential(credential_id, binding)
            revoked.append(credential_id)

    # The next approval mints a new identifier (a new DID per REC), never this one.
    if hasattr(subject, "dataspace_subject_id"):
        subject.dataspace_subject_id = None

    after = await held_credentials(identifiers, did, binding) if registry else HeldCredentials()
    if after.ours:
        return StepResult(
            step,
            FAILED,
            "credential_remains",
            f"{len(after.ours)} credential(s) this community linked still active; release again",
        )
    if after.elsewhere:
        return StepResult(
            step,
            DONE,
            "held_elsewhere",
            f"revoked {len(revoked)}; {after.elsewhere} credential(s) another organisation "
            "linked remain on this DID, so the next community's login sync is refused until "
            "that organisation revokes them",
        )
    if revoked:
        if len(revoked) > 1 or detail is None:
            detail = f"revoked credential(s) {', '.join(revoked)}"
        return StepResult(step, DONE, "revoked", detail)
    return StepResult(
        step, DONE, "no_credential", "no active credential for this community; membership removed"
    )


async def _login(community: str, member_key: str) -> StepResult:
    step = EnablementStep.KEYCLOAK_USER
    if not provisioning.provisioning_enabled():
        return StepResult(step, SKIPPED, "no_provisioning", "no provisioning service is configured")
    outcome = await provisioning.release_login(community, member_key)
    status = SKIPPED if outcome.code == "no_account" else DONE
    return StepResult(step, status, outcome.code, outcome.detail)


async def _registry(community: str, member_key: str, *, was_inactive: bool) -> StepResult:
    step = EnablementStep.REC_REGISTRY_MEMBER
    detail = await rec_registry.deactivate_registry_member(community, member_key)
    if was_inactive:
        return StepResult(
            step, DONE, "deactivated", f"registry member {member_key} was already inactive"
        )
    return StepResult(step, DONE, "deactivated", detail)


# ---------------------------------------------------------------------------
# The release
# ---------------------------------------------------------------------------


async def release(db: AsyncSession, *, community: str, rec_slug: str, member_key: str) -> Release:
    """Release ``(community, member_key)``. Never raises for a step: the steps are the answer.

    Raises :class:`MemberNotFoundError` when the registry has no such member, and
    :class:`~celine.onboarding.services.rec_registry.RegistryUnavailableError`
    when it cannot say — before anything is changed.
    """
    if not settings.rec_registry_url:
        raise rec_registry.RegistryUnavailableError("no REC registry is configured")

    member = await rec_registry.registry_member(community, member_key)
    if member is None:
        raise MemberNotFoundError(f"{community} has no member {member_key!r}")
    username = _value(member, "user_id")
    was_inactive = _value(member, "status") == "inactive"

    submission = await _submission_for(db, rec_slug, member_key)
    subject: _Subject | Submission
    if submission is not None and submission.dataspace_did:
        subject = submission
    else:
        subject = _Subject(
            rec_slug=rec_slug, ref=member_key, dataspace_did=_value(member, "did") or None
        )

    result = Release(
        community=community,
        member_key=member_key,
        source="submission" if submission is not None else "registry",
    )
    runs = {
        EnablementStep.DATASPACE_SHARE: lambda: _share(subject),
        EnablementStep.DATASPACE_IDENTITY: lambda: _identity(subject, username=username),
        EnablementStep.KEYCLOAK_USER: lambda: _login(community, member_key),
        EnablementStep.REC_REGISTRY_MEMBER: lambda: _registry(
            community, member_key, was_inactive=was_inactive
        ),
    }

    failed: set[EnablementStep] = set()
    for step in STEPS:
        waited = WAITS_FOR.get(step)
        if waited in failed:
            outcome = StepResult(
                step,
                BLOCKED,
                f"waits_for_{waited.value}",
                f"not attempted: {waited.value} failed, and undoing this first would "
                "leave it unrepairable; release again",
            )
            failed.add(step)
        else:
            try:
                outcome = await runs[step]()
            except Exception as exc:  # noqa: BLE001 — recorded on the step, retried by calling again
                logger.warning(
                    "Releasing %s/%s: step %s failed: %s", community, member_key, step.value, exc
                )
                outcome = StepResult(
                    step, FAILED, FAILED_CODES[step], f"{type(exc).__name__}: {exc}"[:4000]
                )
                failed.add(step)
        if outcome.status == FAILED:
            failed.add(step)
        outcome.step = step.value
        result.steps.append(outcome)

    if submission is not None:
        await _record_on_rows(db, submission, result)
    await db.commit()

    logger.info(
        "Released %s/%s (%s): %s",
        community,
        member_key,
        result.source,
        ", ".join(f"{s.step}={s.status}:{s.code}" for s in result.steps),
    )
    return result


async def _record_on_rows(db: AsyncSession, submission: Submission, result: Release) -> None:
    """Write each step's outcome on the submission's enablement row, as `revoke` does.

    A step done (or with nothing to do) on a row that held something becomes
    revoked: `pending` with `completed_at`, its reference and invitation cleared.
    A failed step is `failed` with the reason. A blocked step's row is left as it
    is — it still holds what it held. Rows that never ran are not touched.
    """
    rows = await enablement.load_steps(db, submission.id)
    now = datetime.now(UTC)
    for outcome in result.steps:
        row = rows.get(outcome.step)
        if row is None or outcome.status == BLOCKED:
            continue
        if outcome.status == FAILED:
            row.status = EnablementStatus.FAILED
            row.last_error = f"{enablement.REVOKE_FAILED} — {outcome.detail}"[:4000]
            continue
        if row.status in (EnablementStatus.SUCCEEDED, EnablementStatus.FAILED):
            row.status = EnablementStatus.PENDING
            row.detail = outcome.detail
            row.invitation = None
            row.last_error = None
            row.external_ref = None
            row.completed_at = now
