from __future__ import annotations

from dataclasses import dataclass

import httpx

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission


@dataclass(frozen=True)
class KeycloakProvisionResult:
    user_id: str
    username: str
    created: bool


def _normalized_email(submission: Submission) -> str:
    email = (submission.email or "").strip().lower()
    if not email:
        raise ValueError("Cannot create Keycloak user: approved submission has no email")
    return email


def _display_name(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _base_url() -> str:
    base_url = settings.dataspace_keycloak_base_url.strip().rstrip("/")
    if not base_url:
        raise ValueError(
            "DATASPACE_KEYCLOAK_BASE_URL is required when Keycloak provisioning is enabled"
        )
    return base_url


async def provision_keycloak_user(submission: Submission) -> KeycloakProvisionResult | None:
    if not settings.dataspace_keycloak_enabled:
        return None

    email = _normalized_email(submission)
    async with httpx.AsyncClient(base_url=_base_url(), timeout=15) as client:
        token = await _admin_access_token(client)
        existing = await _find_user(client, token, email)

        if existing:
            user_id = str(existing["id"])
            if settings.dataspace_keycloak_update_existing:
                await _update_user(client, token, user_id, submission, email)
            return KeycloakProvisionResult(user_id=user_id, username=email, created=False)

        user_id = await _create_user(client, token, submission, email)
        if settings.dataspace_keycloak_default_password:
            await _set_password(client, token, user_id)
        return KeycloakProvisionResult(user_id=user_id, username=email, created=True)


async def _admin_access_token(client: httpx.AsyncClient) -> str:
    if not settings.dataspace_keycloak_admin_username:
        raise ValueError("DATASPACE_KEYCLOAK_ADMIN_USERNAME is required")
    if not settings.dataspace_keycloak_admin_password:
        raise ValueError("DATASPACE_KEYCLOAK_ADMIN_PASSWORD is required")

    data = {
        "grant_type": "password",
        "client_id": settings.dataspace_keycloak_admin_client_id,
        "username": settings.dataspace_keycloak_admin_username,
        "password": settings.dataspace_keycloak_admin_password,
    }
    if settings.dataspace_keycloak_admin_client_secret:
        data["client_secret"] = settings.dataspace_keycloak_admin_client_secret

    response = await client.post(
        f"/realms/{settings.dataspace_keycloak_admin_realm}/protocol/openid-connect/token",
        data=data,
    )
    if response.status_code >= 400:
        raise ValueError(f"Keycloak admin login failed: {response.text}")

    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise ValueError("Keycloak admin login did not return an access token")
    return str(token)


async def _find_user(
    client: httpx.AsyncClient,
    token: str,
    email: str,
) -> dict[str, object] | None:
    headers = _auth_headers(token)
    for query in ({"username": email, "exact": "true"}, {"email": email, "exact": "true"}):
        response = await client.get(
            f"/admin/realms/{settings.dataspace_keycloak_realm}/users",
            headers=headers,
            params=query,
        )
        if response.status_code >= 400:
            raise ValueError(f"Keycloak user lookup failed: {response.text}")
        users = response.json()
        if users:
            return users[0]
    return None


async def _create_user(
    client: httpx.AsyncClient,
    token: str,
    submission: Submission,
    email: str,
) -> str:
    payload = _user_payload(submission, email)
    if settings.dataspace_keycloak_default_password:
        payload["credentials"] = [_password_payload()]

    response = await client.post(
        f"/admin/realms/{settings.dataspace_keycloak_realm}/users",
        headers=_auth_headers(token),
        json=payload,
    )
    if response.status_code not in {201, 204}:
        raise ValueError(f"Keycloak user creation failed: {response.text}")

    location = response.headers.get("Location", "")
    user_id = location.rstrip("/").split("/")[-1] if location else ""
    if user_id:
        return user_id

    created = await _find_user(client, token, email)
    if not created:
        raise ValueError("Keycloak user was created but could not be found")
    return str(created["id"])


async def _update_user(
    client: httpx.AsyncClient,
    token: str,
    user_id: str,
    submission: Submission,
    email: str,
) -> None:
    response = await client.put(
        f"/admin/realms/{settings.dataspace_keycloak_realm}/users/{user_id}",
        headers=_auth_headers(token),
        json=_user_payload(submission, email),
    )
    if response.status_code >= 400:
        raise ValueError(f"Keycloak user update failed: {response.text}")


async def _set_password(client: httpx.AsyncClient, token: str, user_id: str) -> None:
    response = await client.put(
        f"/admin/realms/{settings.dataspace_keycloak_realm}/users/{user_id}/reset-password",
        headers=_auth_headers(token),
        json=_password_payload(),
    )
    if response.status_code >= 400:
        raise ValueError(f"Keycloak password setup failed: {response.text}")


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _user_payload(submission: Submission, email: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "username": email,
        "email": email,
        "enabled": True,
        "emailVerified": True,
        "requiredActions": [],
        "attributes": {
            "onboarding_ref": [submission.ref],
            "onboarding_rec": [submission.rec_slug],
        },
    }
    first_name = _display_name(submission.first_name)
    last_name = _display_name(submission.last_name)
    if first_name:
        payload["firstName"] = first_name
    if last_name:
        payload["lastName"] = last_name
    return payload


def _password_payload() -> dict[str, object]:
    return {
        "type": "password",
        "value": settings.dataspace_keycloak_default_password,
        "temporary": settings.dataspace_keycloak_temporary_password,
    }


async def disable_keycloak_user(user_id: str) -> None:
    """Disable a login rather than delete it.

    Deleting would take the audit trail on the Keycloak side with it, and a
    disabled account can be re-enabled if the revocation turns out to have been a
    mistake. Erasure of the person's data is a separate act — see the purge path.
    """
    if not settings.dataspace_keycloak_enabled:
        return

    async with httpx.AsyncClient(base_url=_base_url(), timeout=15) as client:
        token = await _admin_access_token(client)
        response = await client.put(
            f"/admin/realms/{settings.dataspace_keycloak_realm}/users/{user_id}",
            json={"enabled": False},
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code >= 400 and response.status_code != 404:
            raise ValueError(
                f"Disabling Keycloak user {user_id} failed "
                f"({response.status_code}): {response.text}"
            )
