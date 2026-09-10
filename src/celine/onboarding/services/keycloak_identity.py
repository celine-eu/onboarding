"""Provisioning a participant's login in Keycloak, as this service.

Every call here is made as `OIDC_CLIENT_ID` — celine's own client, and the one
`celine-policies` grants a fine-grained admin permission over one realm group.
No administrator credential is held anywhere. See `services.service_auth` for
why that client rather than the dataspace's.

**The grant is the group, and the group is the whole reach.** This service may
create a participant *into* `DATASPACE_KEYCLOAK_PARTICIPANTS_GROUP`, and read,
update, disable and reset the password of somebody already in it. Everything
else in the realm — every operator's account, every other service's user — is
403, and so is creating a user in no group at all. That is what makes the
credential safe to hold in the service that faces the public wizard.

Two consequences shape the code below, both measured against Keycloak 26.6.0
rather than read in documentation:

* **There is no user search.** The realm-wide `GET /users?username=…` needs
  `Users: view` over the whole realm, which this grant deliberately withholds,
  and `GET /groups/{id}/members` accepts a `search` parameter but ignores it —
  it answers 200 and returns every member. So finding somebody means paging the
  group and matching here.
* **A create therefore goes first.** Scanning before every create would cost a
  page per hundred participants to discover what is almost always a new user, so
  `POST /users` runs first and Keycloak's `409` is what triggers the scan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission
from celine.onboarding.services.errors import ConfigurationError
from celine.onboarding.services.service_auth import issuer_realm, keycloak_admin_auth_headers

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


#: One page of `GET /groups/{id}/members`. Keycloak's own default is 100, and the
#: endpoint honours `first`/`max` even though it ignores `search`.
_MEMBER_PAGE = 100

#: The groups `celine-policies` creates for the **operator** role hierarchy.
#: `access.rego` reads a realm-level one as a platform-wide grant with no
#: organization check, so none of them can be the participants group — see
#: `participants_group`.
ROLE_HIERARCHY_GROUPS = frozenset({"admins", "managers", "editors", "viewers"})


def participants_group() -> str:
    """The group participants are created in, as a Keycloak group path.

    Not a filing convention: it is the boundary of this service's grant. A user
    created outside it is refused, and so is any call touching somebody who is
    not in it. Community membership is a separate thing entirely and lives where
    it always did — the registry's `Member` row and the Keycloak organization.
    """
    path = settings.dataspace_keycloak_participants_group.strip().rstrip("/")
    if not path:
        raise ConfigurationError(
            "DATASPACE_KEYCLOAK_PARTICIPANTS_GROUP is required when Keycloak "
            "provisioning is enabled: this service may only create a user inside "
            "the group its grant names, so there is no group to create them in"
        )
    return path if path.startswith("/") else f"/{path}"


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
            f" — the token is valid and carries no rights over group "
            f"{settings.dataspace_keycloak_participants_group!r} in realm "
            f"{keycloak_realm()!r}. Grant client {settings.oidc_client_id!r} a "
            "fine-grained admin permission on that group with the scopes "
            "'manage-members', 'manage-membership', 'view-members' and 'view' — "
            "celine-policies declares it in clients.yaml under `admin_permissions`. "
            "Note that a creation is refused with 403 rather than 404 when the group "
            "itself is missing, so check that the realm has been synced."
        )
    return ""


async def provision_keycloak_user(submission: Submission) -> KeycloakProvisionResult | None:
    if not settings.dataspace_keycloak_enabled:
        return None

    email = _normalized_email(submission)
    group = participants_group()
    async with httpx.AsyncClient(base_url=_base_url(), timeout=15) as client:
        headers = await _admin_headers()
        user_id = await _create_user(client, headers, submission, email, group)

        if user_id is not None:
            if settings.dataspace_keycloak_default_password:
                await _set_password(client, headers, user_id)
            return KeycloakProvisionResult(user_id=user_id, username=email, created=True)

        # Keycloak says the name or the address is taken. By whom is the whole
        # question: somebody already in the group is this participant, arriving
        # a second time — a re-approval, or a retried enablement — and somebody
        # outside it is unreachable from here.
        existing = await _find_member(client, headers, email, group)
        if not existing:
            raise _unreachable_duplicate(email, group)

        member_id = str(existing["id"])
        if settings.dataspace_keycloak_update_existing:
            await _update_user(client, headers, member_id, submission, email)
        # The username Keycloak holds, which is not always the email we matched
        # on: the scan matches the *email* too, so a user created by anything
        # other than this service is found under a name of its choosing.
        # Usernames here are not emails by convention — `celine-policies` names
        # participants by their registry member key — so the value is read rather
        # than assumed. Whatever it is, it is what their token will carry, and
        # what the registry needs in `Member.user_id` for them to resolve
        # themselves.
        return KeycloakProvisionResult(
            user_id=member_id,
            username=str(existing.get("username") or email),
            created=False,
        )


def _unreachable_duplicate(email: str, group: str) -> ValueError:
    """The account exists, is not in the group, and cannot be reached from here.

    Not merely unfound: under this grant the service can neither read it, adopt
    it, nor add it to the group. Nothing can be done automatically, so the step
    error carries what a person has to do instead of a bare status code. It says
    nothing about the realm or the client — a REC operator reads this — but it
    does name the group, because that is the fact somebody has to act on.
    """
    logger.warning(
        "Keycloak refused to create %s: the username or email is taken by an account "
        "outside %s, which this service's grant cannot see. Add that account to the "
        "group if it is the same person, or resolve the collision.",
        email,
        group,
    )
    return ValueError(
        "A Keycloak account already uses this email address and is not in the "
        f"participants group ({group}), so this service can neither see it nor "
        "adopt it. A platform operator has to add that account to the group, or "
        "resolve the collision, before this step can succeed."
    )


async def _admin_headers() -> dict[str, str]:
    """This service's own token, presented to the Keycloak Admin API.

    There is no admin login step any more. Provisioning used to authenticate as
    a realm administrator — `grant_type=password` against the master realm —
    which is a credential that can do anything to any realm, held by the service
    that faces the public wizard, to create users in one realm. This service's
    own client carries the two roles the job needs instead.
    """
    return {**await keycloak_admin_auth_headers(), "Content-Type": "application/json"}


async def _group_id(client: httpx.AsyncClient, headers: dict[str, str], group: str) -> str:
    """The group's uuid, which every member call is addressed by.

    `group-by-path` is the only route to it under this grant, and it needs the
    `view` scope on the group alongside the three member scopes: without it —
    and without any realm-wide role — this call, `GET /groups`, `GET /groups?
    search=` and even `GET /groups/{id}` on the administered group are all 403.
    """
    response = await client.get(
        f"/admin/realms/{keycloak_realm()}/group-by-path{group}",
        headers=headers,
    )
    if response.status_code == 404:
        # The realm has not been given the group this service's grant is scoped
        # to. That is a deployment's own to fix, not an operator's — and it is
        # worth reporting distinctly, because the same absence surfaces on a
        # creation as an indistinguishable 403.
        raise ConfigurationError(
            f"Keycloak group {group!r} does not exist in realm {keycloak_realm()!r}. "
            "It is created by celine-policies' `keycloak sync` from the "
            "`admin_permissions` block that grants this service its rights over it; "
            "a realm that has not been synced has neither."
        )
    if response.status_code >= 400:
        raise _refused(f"looking up group {group}", response)
    return str(response.json()["id"])


async def _find_member(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    email: str,
    group: str,
) -> dict[str, object] | None:
    """Scan the group for a participant, by username or email.

    A scan because there is nothing else: the realm-wide user search is 403 under
    this grant, and `GET /groups/{id}/members` accepts `search` and `exact` and
    ignores both — it answers 200 with every member either way. So the filtering
    happens here, one page of members at a time.

    Only reached when a creation came back 409, which is why paging the whole
    group is affordable: it is the second visit of a participant who already has
    a login, not the first visit of one who does not.
    """
    group_id = await _group_id(client, headers, group)
    first = 0
    while True:
        response = await client.get(
            f"/admin/realms/{keycloak_realm()}/groups/{group_id}/members",
            headers=headers,
            params={"first": first, "max": _MEMBER_PAGE, "briefRepresentation": "true"},
        )
        if response.status_code >= 400:
            raise _refused("group member lookup", response)
        members = response.json()
        for member in members:
            names = {
                (member.get("username") or "").strip().lower(),
                (member.get("email") or "").strip().lower(),
            }
            if email in names:
                return member
        if len(members) < _MEMBER_PAGE:
            return None
        first += _MEMBER_PAGE


async def _create_user(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    submission: Submission,
    email: str,
    group: str,
) -> str | None:
    """Create the participant, or report that the name is already taken.

    `None` is a 409 and not a failure: Keycloak checks uniqueness before it
    checks containment, so the answer is the same whether the account that holds
    the name is in this service's group or somewhere it can never look. Which of
    the two it is decides what happens next, and only the caller can find out.
    """
    payload = _user_payload(submission, email, group)
    if settings.dataspace_keycloak_default_password:
        payload["credentials"] = [_password_payload()]

    response = await client.post(
        f"/admin/realms/{keycloak_realm()}/users",
        headers=headers,
        json=payload,
    )
    if response.status_code == 409:
        return None
    if response.status_code == 403:
        # Two different faults answer 403 to a creation, and Keycloak
        # distinguishes neither: a grant that does not cover this group, and a
        # realm that has no such group at all. `group-by-path` is the only call
        # that tells them apart, and it is worth one more request on a path that
        # has already failed — a deployment whose realm was never synced would
        # otherwise be told its permissions are wrong.
        await _diagnose_creation_refusal(client, headers, group)
    if response.status_code not in {201, 204}:
        raise _refused("user creation", response)

    location = response.headers.get("Location", "")
    user_id = location.rstrip("/").split("/")[-1] if location else ""
    if user_id:
        return user_id

    created = await _find_member(client, headers, email, group)
    if not created:
        raise ValueError("Keycloak user was created but could not be found")
    return str(created["id"])


async def _diagnose_creation_refusal(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    group: str,
) -> None:
    """Raise `ConfigurationError` when the group the creation named is missing.

    Returns quietly otherwise, including when this lookup is itself refused —
    that is a grant without the `view` scope, which is the caller's own 403 to
    report and not a second one to raise here.
    """
    try:
        await _group_id(client, headers, group)
    except ConfigurationError:
        raise
    except Exception:  # noqa: BLE001 — the caller's refusal is the one that matters
        return


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
    payload = _user_payload(submission, email, participants_group())
    payload.pop("username", None)
    # And not their membership either. A `PUT` carrying `groups` is accepted with
    # a 204 and silently ignored — measured, including a body naming a *different*
    # group, which left the membership untouched. Sending an instruction Keycloak
    # discards would read as if this call maintained membership, and it does not.
    payload.pop("groups", None)
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


def _user_payload(submission: Submission, email: str, group: str) -> dict[str, object]:
    """The user to create — and the group to create them in.

    `groups` is not filing, it is what makes the creation permitted at all:
    creating a user in *no* group is a realm-wide act, and this service's grant
    reaches one group. Without this key `POST /users` is 403, and so it is when
    the key names a group the grant does not cover.
    """
    payload: dict[str, object] = {
        "username": email,
        "email": email,
        "groups": [group],
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
