from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission
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
    if settings.dataspace_linked_participant_did:
        body["linked_participant_did"] = settings.dataspace_linked_participant_did
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

    if keycloak_user_id and keycloak_realm:
        await _sync_keycloak(
            base_url,
            headers,
            did=submission.dataspace_did,
            keycloak_user_id=keycloak_user_id,
            keycloak_realm=keycloak_realm,
            email=submission.email,
            credential_id=submission.dataspace_vc_id,
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
                    return
                last_error = ValueError(
                    f"KC sync failed ({resp.status_code}): {resp.text}"
                )
        except httpx.HTTPError as exc:
            last_error = exc

        if attempt < _KC_SYNC_MAX_RETRIES:
            logger.warning("KC sync attempt %d/%d failed, retrying", attempt, _KC_SYNC_MAX_RETRIES)

    logger.error("KC sync failed after %d attempts, revoking credential %s", _KC_SYNC_MAX_RETRIES, credential_id)
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
