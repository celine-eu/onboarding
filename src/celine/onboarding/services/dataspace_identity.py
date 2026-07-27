from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any

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


def _email_subject_id(email: str | None) -> str:
    normalized = (email or "").strip().lower()
    if not normalized:
        raise ValueError("Cannot build dataspace subject id from email: value is empty")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"email-{digest}"


def _subject_id(submission: Submission) -> str:
    source = settings.dataspace_subject_source.strip().lower()
    if source in {"submission_ref", "ref"}:
        value = submission.ref
    elif source in {"email_hash", "email"}:
        value = _email_subject_id(submission.email)
    else:
        raise ValueError(
            "Unsupported DATASPACE_SUBJECT_SOURCE. Use email_hash or submission_ref."
        )

    subject_id = value.strip().lower()
    if not subject_id:
        raise ValueError(f"Cannot build dataspace subject id from {source}: value is empty")
    if not _SAFE_SUBJECT.fullmatch(subject_id):
        raise ValueError(
            "Dataspace subject id may contain only letters, digits, dot, underscore, "
            "plus and hyphen"
        )
    return subject_id


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


async def emit_data_disclosed(
    *,
    recipient_ref: str,
    purpose: list[str] | None = None,
    columns: list[str] | None = None,
    subject_count: int | None = None,
    source_ref: str | None = None,
    consent_snapshot_hash: str | None = None,
    agreement_ref: str | None = None,
    event_id: str | None = None,
    rec_slug: str | None = None,
) -> bool:
    """Record an offline data disclosure (a CSV export) in ds-provenance.

    Non-fatal: a failed provenance emit must never fail the export it documents,
    so this logs and returns False rather than raising.  Carries **codes, DIDs
    and hashes only, never PII** — ``columns`` are field *names*, not values, and
    ``consent_snapshot_hash`` is a recomputable fingerprint of the consent state.
    """
    if not settings.ds_provenance_url:
        return False

    # The disclosing agent is the REC that holds the data, so it is per-REC like
    # every other dataspace binding. Without a slug there is nobody to name, and
    # an unresolvable one must not fail the export this call only documents.
    disclosed_by: str | None = None
    if rec_slug:
        try:
            binding = template_service.dataspace_binding(rec_slug)
            disclosed_by = binding.organization_did or binding.organization or None
        except (KeyError, ValueError):
            logger.warning(
                "No dataspace binding for REC %r; DataDisclosed will not name a "
                "disclosing agent",
                rec_slug,
            )
    base_url = settings.ds_provenance_url.rstrip("/")
    payload: dict[str, Any] = {
        "event_type": "DataDisclosed",
        "event_id": event_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "recipient_ref": recipient_ref,
        "purpose": purpose or [],
        "columns": columns or [],
        "subject_count": subject_count,
        "source_ref": source_ref,
        "disclosed_by": disclosed_by,
        "consent_snapshot_hash": consent_snapshot_hash,
        "agreement_ref": agreement_ref,
    }
    try:
        headers = await _auth_headers()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{base_url}/prov/events", json=payload, headers=headers
            )
        if resp.status_code >= 400:
            logger.warning(
                "DataDisclosed emit failed (%s): %s", resp.status_code, resp.text
            )
            return False
    except Exception:
        logger.exception("DataDisclosed emit failed for recipient %s", recipient_ref)
        return False
    return True


async def organization_exists(org_alias: str) -> bool | None:
    """Is *org_alias* a known owner in the identity registry?

    Returns ``True``/``False``, or ``None`` when the registry could not be
    reached. The distinction is the point: a registry that answers "no such
    owner" is a configuration error worth refusing to start on, while a registry
    that is briefly down is not — coupling boot to another service's availability
    would turn a transient outage into an outage here.
    """
    if not settings.identity_registry_url:
        return None
    base_url = settings.identity_registry_url.rstrip("/")
    try:
        headers = await _auth_headers()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{base_url}/admin/owners/{org_alias}", headers=headers
            )
    except Exception:
        logger.warning(
            "Could not reach the identity registry to verify organization %r",
            org_alias,
        )
        return None
    if resp.status_code == 404:
        return False
    if resp.status_code >= 400:
        logger.warning(
            "Identity registry answered %s verifying organization %r",
            resp.status_code,
            org_alias,
        )
        return None
    return True


async def provision_user_identity(
    submission: Submission,
    *,
    keycloak_user_id: str | None = None,
    keycloak_realm: str | None = None,
) -> None:
    if not settings.dataspace_vc_enabled:
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

    base_url = settings.identity_registry_url.rstrip("/")
    subject_id = _subject_id(submission)
    headers = await _auth_headers()

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
    submission.dataspace_did = evidence.get("subjectDid")
    submission.dataspace_vc_id = evidence.get("credentialId")
    submission.dataspace_vc_issued_at = _parse_generated_at(evidence.get("generatedAt"))

    if not submission.dataspace_did or not submission.dataspace_vc_id:
        raise ValueError(
            "Identity-registry response is missing subjectDid or credentialId"
        )

    org_alias = binding.organization
    if org_alias:
        await _register_membership(
            base_url, headers, submission.dataspace_did, org_alias, binding.membership_role
        )

    if keycloak_user_id and keycloak_realm:
        await _sync_keycloak(
            base_url,
            headers,
            did=submission.dataspace_did,
            keycloak_user_id=keycloak_user_id,
            keycloak_realm=keycloak_realm,
            email=submission.email,
            credential_id=submission.dataspace_vc_id,
            organization_alias=org_alias,
        )

    # Standing data-sharing consent, if the person opted in. Deliberately the
    # LAST step and deliberately non-fatal: a failed share is recoverable, and
    # tearing down a valid identity because a consent row didn't write is the
    # wrong trade (§3.5). The rollback above does not extend here.
    if settings.ds_connector_url and submission.data_sharing_consent:
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
