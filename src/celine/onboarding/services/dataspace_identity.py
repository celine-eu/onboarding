from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from dataclasses import dataclass

import httpx

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission
from celine.onboarding.services import template_service
from celine.sdk.auth import OidcClientCredentialsProvider

logger = logging.getLogger(__name__)

_SAFE_SUBJECT = re.compile(r"^[A-Za-z0-9._+-]{1,128}$")

_token_provider: OidcClientCredentialsProvider | None = None

_KC_SYNC_MAX_RETRIES = 3


def _get_token_provider() -> OidcClientCredentialsProvider:
    global _token_provider
    if _token_provider is None:
        if not settings.oidc_base_url:
            raise ValueError("OIDC_BASE_URL is required when dataspace VC is enabled")
        _token_provider = OidcClientCredentialsProvider(
            base_url=settings.oidc_base_url,
            client_id=settings.ds_onboarding_client_id,
            client_secret=settings.ds_onboarding_client_secret,
        )
    return _token_provider


def _submission_ref_subject_id(submission: Submission) -> str:
    value = (submission.ref or "").strip().lower()
    if not value:
        raise ValueError("Cannot build dataspace subject id from submission_ref: value is empty")
    if not _SAFE_SUBJECT.fullmatch(value):
        raise ValueError(
            "Dataspace subject id may contain only letters, digits, dot, underscore, "
            "plus and hyphen"
        )
    return value


def _parse_generated_at(value: Any) -> datetime:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def _auth_headers() -> dict[str, str]:
    token = await _get_token_provider().get_token()
    return {"Authorization": f"Bearer {token.access_token}"}


async def record_disclosure(
    *,
    offer_id: str,
    recipient_ref: str,
    purpose: list[str] | None = None,
    columns: list[str] | None = None,
    subject_count: int | None = None,
    source_ref: str | None = None,
    agreement_ref: str | None = None,
    event_id: str | None = None,
    rec_slug: str | None = None,
) -> list[dict[str, Any]]:
    """Record an outbound disclosure with the connector, before it happens.

    Replaces a direct ``POST {DS_PROVENANCE_URL}/prov/events``. That call had no
    ``dataset_id`` and the provenance service now requires one, so every emit was
    answered 422 and discarded — silently, because the emit was non-fatal. The
    export went out and nothing recorded it.

    **The connector computes the consent snapshot hash**, which is the whole
    reason the route exists: the hash is a fingerprint of *its* consent rows, and
    a caller asserting one would be asserting a consent state it cannot read.

    **Named by offer, expanded by the connector.** A POD list is scoped to one
    sharing offer, never to a dataset. The connector resolves the offer to the
    datasets it reaches and records one ``DataDisclosed`` per dataset, deriving a
    per-dataset event id from ``event_id`` so a retry stays idempotent.

    Returns one entry per dataset — ``dataset_id``, ``consent_snapshot_hash``,
    ``granted_party_count``. **Every entry matters**: the response deliberately
    does not flatten to top-level keys even when the offer resolves to a single
    dataset, so that a caller cannot read one and be right today and wrong the
    day a second dataset declares the offer.

    **Fatal, unlike the emit it replaces.** The old call documented something that
    had already happened, so losing it was worse than failing. This one runs
    *before* the handover, so a refusal means the disclosure does not happen —
    the answer that leaves no unrecorded handover. Callers must not write the
    file if this raises.
    """
    if not settings.ds_connector_url:
        raise RuntimeError(
            "DS_CONNECTOR_URL is not configured, so this disclosure cannot be "
            "recorded — and an unrecorded handover is what this call prevents."
        )

    # The disclosing agent is the REC that holds the data, so it is per-REC like
    # every other dataspace binding.
    disclosed_by: str | None = None
    if rec_slug:
        try:
            binding = template_service.dataspace_binding(rec_slug)
            disclosed_by = binding.organization_did or binding.organization or None
        except (KeyError, ValueError):
            logger.warning(
                "No dataspace binding for REC %r; the disclosure will not name a "
                "disclosing agent",
                rec_slug,
            )

    base_url = settings.ds_connector_url.rstrip("/")
    payload: dict[str, Any] = {
        "offer_id": offer_id,
        "recipient_ref": recipient_ref,
        "purpose": purpose or [],
        "columns": columns or [],
        "subject_count": subject_count,
        "source_ref": source_ref,
        "disclosed_by": disclosed_by,
        "agreement_ref": agreement_ref,
        "event_id": event_id,
    }

    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base_url}/admin/disclosure", json=payload, headers=headers
        )

    if resp.status_code >= 400:
        # 502 is the partial case and the connector names what it already
        # recorded. Retry with the **same** event_id: the per-dataset derivation
        # makes that idempotent, where a fresh one would record a second copy of
        # what this failure already wrote.
        raise RuntimeError(
            f"Disclosure was not recorded ({resp.status_code}), so the data must "
            f"not be handed over: {resp.text}"
        )

    body = resp.json()
    disclosures = body.get("disclosures") or []
    if not disclosures:
        raise RuntimeError(
            f"The connector recorded no disclosure for offer {offer_id!r}; "
            "refusing to hand over data that nothing describes."
        )
    return disclosures


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
        status = str(resp.json().get("status") or "").strip() or None
    except ValueError:
        # Found, but the body was not readable. Do not invent a status: an
        # absent one must not read as "not verified" and refuse boot.
        status = None
    return OwnerCheck(found=True, status=status)


async def _resolve_or_derive_subject(
    base_url: str, headers: dict[str, str], email: str,
) -> str:
    """Resolve or derive a subject_id via the identity-registry.

    The IR is the sole authority on email→subject_id mapping: it either
    returns an existing one or derives a new one keyed by its own secret.
    A failure here is fatal — the credential issuance that follows requires
    the same service, so swallowing the error would only delay it.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{base_url}/users/resolve",
            params={"email": email, "derive": "true"},
            headers=headers,
        )
    if resp.status_code == 200:
        sid = resp.json().get("subject_id")
        if sid:
            return sid
    raise ValueError(
        f"Subject id derivation failed: identity registry returned {resp.status_code}"
    )


async def provision_user_identity(
    submission: Submission,
    *,
    keycloak_user_id: str | None = None,
    keycloak_realm: str | None = None,
    provision_shares: bool = True,
) -> None:
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

    base_url = settings.identity_registry_url.rstrip("/")
    headers = await _auth_headers()

    source = settings.dataspace_subject_source.strip().lower()
    if source in {"email_hash", "email"}:
        if not submission.email:
            raise ValueError("Cannot derive subject id: submission has no email")
        subject_id = await _resolve_or_derive_subject(
            base_url, headers, submission.email,
        )
    elif source in {"submission_ref", "ref"}:
        subject_id = _submission_ref_subject_id(submission)
    else:
        raise ValueError(
            "Unsupported DATASPACE_SUBJECT_SOURCE. Use email_hash or submission_ref."
        )

    allowed_actions = [
        a.strip() for a in settings.dataspace_allowed_actions.split(",") if a.strip()
    ]

    body: dict[str, Any] = {
        "subject_id": subject_id,
        "role": settings.dataspace_user_role,
        "ttl_days": settings.dataspace_vc_ttl_days,
    }
    if binding.linked_participant_did:
        body["linked_participant_did"] = binding.linked_participant_did
    if allowed_actions:
        body["allowed_actions"] = allowed_actions

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base_url}/admin/credentials/data-subject",
            json=body,
            headers=headers,
        )
        if resp.status_code >= 400:
            raise ValueError(f"Credential issuance failed ({resp.status_code}): {resp.text}")

        evidence = resp.json()

    submission.dataspace_subject_id = subject_id

    did: str | None = evidence.get("subjectDid")
    cred_id: str | None = evidence.get("credentialId")
    if not did or not cred_id:
        raise ValueError(
            "Identity-registry response is missing subjectDid or credentialId"
        )

    submission.dataspace_did = did
    submission.dataspace_vc_id = cred_id
    submission.dataspace_vc_issued_at = _parse_generated_at(evidence.get("generatedAt"))

    org_alias = binding.organization
    if org_alias:
        await _register_membership(
            base_url, headers, did, org_alias, binding.membership_role
        )

    if keycloak_user_id and keycloak_realm:
        await _sync_keycloak(
            base_url,
            headers,
            did=did,
            keycloak_user_id=keycloak_user_id,
            keycloak_realm=keycloak_realm,
            email=submission.email,
            credential_id=cred_id,
            organization_alias=org_alias,
        )

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


async def provision_user_shares(
    submission: Submission, *, raise_on_error: bool = False
) -> bool:
    """Push the subject's standing data-sharing consent to the connector.

    Called at the end of :func:`provision_user_identity` and again by the admin
    retry endpoint.  Names an ``offer_id`` per recorded offer — never a dataset —
    so the connector expands each into the datasets the offer describes and the
    onboarding config can never drift from what the person read.

    ``raise_on_error`` is False on the approval path (a failure must not fail
    approval) and True on explicit retry (the operator wants to see it fail).
    Returns whether every offer was provisioned.  Idempotent: the connector's
    ``set_subject_data_sharing`` returns the existing row on a re-run.
    """
    if not settings.ds_connector_url:
        return False
    if not submission.data_sharing_consent:
        return False
    if not submission.dataspace_did:
        logger.warning("Cannot provision shares for %s: no dataspace DID", submission.ref)
        return False

    offer_ids = list(submission.data_sharing_consent_offer_ids or [])
    if not offer_ids:
        logger.warning("data_sharing_consent set but no offers recorded for %s", submission.ref)
        if raise_on_error:
            raise ValueError("No data-sharing offers recorded for this submission")
        return False

    problems = _evidence_problems(submission)
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

    connector_url = settings.ds_connector_url.rstrip("/")
    headers = await _auth_headers()
    accepted_at = (
        submission.data_sharing_consent_at.isoformat()
        if submission.data_sharing_consent_at
        else None
    )

    failures: list[str] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for offer_id in offer_ids:
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
            try:
                resp = await client.post(
                    f"{connector_url}/consent/admin/shares",
                    json={
                        "subject_id": submission.dataspace_did,
                        "offer_id": offer_id,
                        "enabled": True,
                        "legal_basis": legal_basis,
                    },
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                logger.error("Share provisioning for offer %s failed: %s", offer_id, exc)
                failures.append(f"{offer_id}: {exc}")
                continue
            if resp.status_code >= 400:
                logger.error(
                    "Share provisioning for offer %s failed (%s): %s",
                    offer_id,
                    resp.status_code,
                    resp.text,
                )
                failures.append(f"{offer_id}: {resp.status_code} {resp.text}")

    ok = not failures
    submission.share_provisioned = ok
    if failures and raise_on_error:
        raise ValueError("Share provisioning failed: " + "; ".join(failures))
    return ok


async def _register_membership(
    base_url: str,
    headers: dict[str, str],
    did: str,
    org_alias: str,
    membership_role: str,
) -> None:
    """Register the user DID as a member of the REC organization.

    Membership is what the ds consent endpoints check, so a user without it holds a
    valid credential but cannot manage data sharing.

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
        "role": membership_role,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{base_url}/admin/memberships", json=body, headers=headers
        )

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
        raise ValueError(
            f"Membership registration failed ({resp.status_code}): {resp.text}"
        )


async def _delete_membership(
    base_url: str, headers: dict[str, str], did: str, org_alias: str
) -> None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.delete(
                f"{base_url}/admin/memberships/{did}/{org_alias}", headers=headers
            )
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


async def _sync_keycloak(
    base_url: str,
    headers: dict[str, str],
    *,
    did: str,
    keycloak_user_id: str,
    keycloak_realm: str,
    email: str | None,
    credential_id: str,
    organization_alias: str = "",
) -> None:
    sync_body = {
        "did": did,
        "keycloak_realm": keycloak_realm,
        "keycloak_user_id": keycloak_user_id,
    }
    if email:
        sync_body["email"] = email

    last_error: Exception | None = None
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
                last_error = ValueError(
                    f"KC sync failed ({resp.status_code}): {resp.text}"
                )
        except httpx.HTTPError as exc:
            last_error = exc

        if attempt < _KC_SYNC_MAX_RETRIES:
            logger.warning("KC sync attempt %d/%d failed, retrying", attempt, _KC_SYNC_MAX_RETRIES)

    logger.error("KC sync failed after %d attempts, revoking credential %s", _KC_SYNC_MAX_RETRIES, credential_id)
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
    ) from last_error


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
        resp = await client.delete(
            f"{base_url}/admin/credentials/{credential_id}", headers=headers
        )
        # 404 is success for this purpose: the credential is gone either way, and
        # refusing to clear the local columns would make the state unrepairable.
        if resp.status_code >= 400 and resp.status_code != 404:
            raise ValueError(
                f"Credential revocation failed ({resp.status_code}): {resp.text}"
            )

    submission.dataspace_vc_id = None
    submission.dataspace_did = None
    submission.dataspace_vc_issued_at = None
    submission.share_provisioned = False
    return f"revoked credential {credential_id}"
