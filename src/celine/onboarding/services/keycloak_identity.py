"""Provisioning a participant's login in Keycloak, as this service.

Every call here is made with the app's own service-account token — see
`services.service_auth`. It needs exactly two realm-management roles,
`manage-users` and `view-users`, and holds no administrator credential.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission
from celine.onboarding.services.errors import ConfigurationError
from celine.onboarding.services.service_auth import issuer_realm, service_auth_headers

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KeycloakProvisionResult:
    user_id: str
    #: The value the participant authenticates as, read back from Keycloak rather
    #: than assumed. It is what the REC registry stores as ``Member.user_id``,
    #: because that is what a token's ``preferred_username`` carries.
    username: str
    created: bool


def _normalized_email(submission: Submission) -> str:
    email = (submission.email or "").strip().lower()
    if not email:
        raise ValueError("Cannot create Keycloak user: approved submission has no email")
    return email


def keycloak_username(submission: Submission) -> str | None:
    """The username this service *would* create for a submission, or ``None``.

    A proxy, not an observation: it is the normalised email, which is what
    :func:`provision_keycloak_user` sets on every user it creates. Callers that
    can have the real value — anything running after provisioning in the same
    pass — should use the ``username`` it returned instead, because a user that
    already existed may authenticate under another convention entirely.
    """
    email = (getattr(submission, "email", "") or "").strip().lower()
    return email or None


def _display_name(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _base_url() -> str:
    """Where the Admin API is, defaulting to where the token came from.

    Not a convenience: Keycloak checks a token's `iss` against the address the
    request arrived on, so presenting a token minted at one hostname to the
    Admin API at another is rejected — with a bare 401, before roles are looked
    at. Deriving the origin from `OIDC_BASE_URL` makes the addresses agree by
    construction. Override it only where Keycloak is configured to accept the
    other address for the same issuer.
    """
    explicit = settings.dataspace_keycloak_base_url.strip().rstrip("/")
    if explicit:
        return explicit
    issuer = settings.oidc_base_url.strip()
    if "/realms/" in issuer:
        return issuer.split("/realms/")[0].rstrip("/")
    raise ConfigurationError(
        "DATASPACE_KEYCLOAK_BASE_URL is required when OIDC_BASE_URL is not a Keycloak issuer"
    )


def keycloak_realm() -> str:
    """The realm whose users this service provisions.

    Unset means the realm `OIDC_BASE_URL` issues from, and that is the useful
    default rather than a convenience: provisioning presents this service's own
    client-credentials token, which administers the realm that minted it and no
    other. Naming one in a default would name somebody's deployment; deriving it
    names this one.
    """
    explicit = settings.dataspace_keycloak_realm.strip()
    if explicit:
        return explicit
    derived = issuer_realm(settings.oidc_base_url)
    if not derived:
        raise ConfigurationError(
            "DATASPACE_KEYCLOAK_REALM is required when OIDC_BASE_URL names no realm"
        )
    return derived


def _refused(action: str, response: httpx.Response) -> ValueError:
    """Report a refusal by its status, and log what Keycloak said.

    The body is Keycloak's, written for whoever runs Keycloak. It reaches the
    console as the text of a failed enablement step, so what it says about the
    realm, the client or the request would be told to every REC operator with a
    review queue. The status code is what they can act on; the rest is a log
    line for the deployment.
    """
    logger.warning(
        "Keycloak %s failed (%s): %s%s",
        action,
        response.status_code,
        response.text,
        _diagnosis(response.status_code),
    )
    return ValueError(f"Keycloak {action} failed ({response.status_code})")


def _diagnosis(status_code: int) -> str:
    """The two refusals this service earns, and what each one means.

    They are worth naming because they look alike and are nothing alike: one is
    a credential the realm will not read, the other a credential it read and
    found empty.
    """
    if status_code == 401:
        return (
            " — the Admin API rejected the token outright, which is what Keycloak does "
            "when the address it arrived on is not the one that minted it. Check that "
            f"DATASPACE_KEYCLOAK_BASE_URL ({_base_url()}) is an address Keycloak serves "
            f"the issuer OIDC_BASE_URL ({settings.oidc_base_url}) from."
        )
    if status_code == 403:
        return (
            f" — the token is valid and carries no rights over realm {keycloak_realm()!r}. "
            f"Grant client {settings.ds_onboarding_client_id!r} the realm-management roles "
            "'manage-users' and 'view-users' on its service account."
        )
    return ""


async def provision_keycloak_user(submission: Submission) -> KeycloakProvisionResult | None:
    if not settings.dataspace_keycloak_enabled:
        return None

    email = _normalized_email(submission)
    async with httpx.AsyncClient(base_url=_base_url(), timeout=15) as client:
        headers = await _admin_headers()
        existing = await _find_user(client, headers, email)

        if existing:
            user_id = str(existing["id"])
            if settings.dataspace_keycloak_update_existing:
                await _update_user(client, headers, user_id, submission, email)
            # The username Keycloak holds, which is not always the email we
            # asked by: `_find_user`'s second query matches on the *email*, so a
            # user created by anything other than this service can come back
            # under a name of its choosing. Usernames here are not emails by
            # convention — `celine-policies` names participants by their registry
            # member key — so the value is read rather than assumed. Whatever it
            # is, it is what their token will carry, and what the registry needs
            # in `Member.user_id` for them to resolve themselves.
            return KeycloakProvisionResult(
                user_id=user_id,
                username=str(existing.get("username") or email),
                created=False,
            )

        user_id = await _create_user(client, headers, submission, email)
        if settings.dataspace_keycloak_default_password:
            await _set_password(client, headers, user_id)
        return KeycloakProvisionResult(user_id=user_id, username=email, created=True)


async def _admin_headers() -> dict[str, str]:
    """This service's own token, presented to the Keycloak Admin API.

    There is no admin login step any more. Provisioning used to authenticate as
    a realm administrator — `grant_type=password` against the master realm —
    which is a credential that can do anything to any realm, held by the service
    that faces the public wizard, to create users in one realm. The service
    account it already uses for every other outbound call carries the two roles
    the job needs instead.
    """
    return {**await service_auth_headers(), "Content-Type": "application/json"}


async def _find_user(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    email: str,
) -> dict[str, object] | None:
    for query in ({"username": email, "exact": "true"}, {"email": email, "exact": "true"}):
        response = await client.get(
            f"/admin/realms/{keycloak_realm()}/users",
            headers=headers,
            params=query,
        )
        if response.status_code >= 400:
            raise _refused("user lookup", response)
        users = response.json()
        if users:
            return users[0]
    return None


async def _create_user(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    submission: Submission,
    email: str,
) -> str:
    payload = _user_payload(submission, email)
    if settings.dataspace_keycloak_default_password:
        payload["credentials"] = [_password_payload()]

    response = await client.post(
        f"/admin/realms/{keycloak_realm()}/users",
        headers=headers,
        json=payload,
    )
    if response.status_code not in {201, 204}:
        raise _refused("user creation", response)

    location = response.headers.get("Location", "")
    user_id = location.rstrip("/").split("/")[-1] if location else ""
    if user_id:
        return user_id

    created = await _find_user(client, headers, email)
    if not created:
        raise ValueError("Keycloak user was created but could not be found")
    return str(created["id"])


async def _update_user(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    user_id: str,
    submission: Submission,
    email: str,
) -> None:
    """Fill in the profile of a user who already existed — but not their username.

    `username` is deliberately absent from the body. This user may have been
    created elsewhere under another convention, and renaming their login is not
    what "update existing" is for: it would change what they type to sign in, and
    it would silently invalidate the `user_id` any registry row already holds for
    them. What is safe to refresh is the profile: email, names, verified flags.
    """
    payload = _user_payload(submission, email)
    payload.pop("username", None)
    response = await client.put(
        f"/admin/realms/{keycloak_realm()}/users/{user_id}",
        headers=headers,
        json=payload,
    )
    if response.status_code >= 400:
        raise _refused("user update", response)


async def _set_password(client: httpx.AsyncClient, headers: dict[str, str], user_id: str) -> None:
    response = await client.put(
        f"/admin/realms/{keycloak_realm()}/users/{user_id}/reset-password",
        headers=headers,
        json=_password_payload(),
    )
    if response.status_code >= 400:
        raise _refused("password setup", response)


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
        response = await client.put(
            f"/admin/realms/{keycloak_realm()}/users/{user_id}",
            json={"enabled": False},
            headers=await _admin_headers(),
        )
        if response.status_code >= 400 and response.status_code != 404:
            raise _refused(f"disabling user {user_id}", response)
