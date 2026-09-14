"""Provisioning a participant's login — by asking the service that owns the realm.

**This service holds no Keycloak grant.** It used to: it administered the members
of one realm group with its own client, created participants into
`/participants`, and paged that group to find somebody again because the
realm-wide user search was deliberately withheld from it. All of that is gone.
``../celine-policies`` runs a provisioning service that is the only writer of
participant accounts, and this module is a client of it — two calls over HTTP,
authenticated with a scope like every other outbound call here.

Why the job moved rather than being narrowed further: the group-scoped grant was
the narrowest shape Keycloak can express and it was still wrong twice over. A
service facing the public wizard held admin rights over accounts, and it could
not finish the job anyway — organization membership is the Organizations API,
which no fine-grained permission reaches, so participants landed in a group and
in no organization, which is the claim every org-scoped policy resolves them by.
See ``docs/decisions/ADR-0004``.

## What that buys, and what it costs

Gone from here: the Admin API, the ``409``-then-scan dance, the group that was a
permission boundary, the two refusals (401 for an issuer mismatch, 403 for a
grant) that needed a paragraph of diagnosis each, and the unreachable-duplicate
case — an account outside the group that this service could neither see nor
adopt. The provisioning service has realm-wide reach, so for it there is no
outside.

The cost is one hop and one new coupling: ``(community, key)``. The provisioning
service keys a participant on the community alias and the registry member key,
which are exactly the pair :mod:`~celine.onboarding.services.rec_registry`
already writes — ``rec_registry.community`` from the REC manifest, and
``submission.ref``. The consequence is in :func:`participant_community`: a REC
declaring no registry binding has no community to key on, so it gets no login.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, get_args

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import ParticipantLocale, Submission
from celine.onboarding.services.errors import ConfigurationError
from celine.onboarding.services.service_auth import issuer_realm

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParticipantProvisionResult:
    """What the provisioning service answered, under this repository's names."""

    #: The Keycloak **uuid**. The dataspace step hands it to the identity
    #: registry. Not the registry's ``Member.user_id``, which is a username —
    #: the two names collide across this seam and the collision has cost a
    #: defect, so they are never both called ``user_id`` in one scope here.
    user_id: str
    #: The value the participant authenticates as, read back from Keycloak by
    #: the provisioning service rather than computed by anybody. It is what the
    #: REC registry stores as ``Member.user_id``, because that is what a token's
    #: ``preferred_username`` carries.
    username: str
    created: bool
    #: The provisioning service's reason code for the invitation this call asked
    #: for: ``not_requested | sent | has_password | not_on_dev_list |
    #: account_disabled | cooldown | send_failed | no_email``. Recorded verbatim
    #: on the step row, where the console translates it. ``None`` only for a
    #: result built without one (tests).
    invitation: str | None = None

    @property
    def invited(self) -> bool:
        """Whether an invitation to set a password went out in this call."""
        return self.invitation == "sent"


_client: Any | None = None


def _get_client():
    """The provisioning client, built once.

    Presents ``OIDC_CLIENT_ID`` — celine's own client, which
    ``../celine-policies`` grants ``provisioning.participants.write``. Not the
    dataspace's client: giving somebody a login in the celine realm is celine
    business, and it was celine's client that used to do it directly.
    """
    global _client
    if _client is None:
        from celine.sdk.provisioning import ProvisioningClient

        from celine.onboarding.services.service_auth import celine_token_provider

        _client = ProvisioningClient(
            base_url=provisioning_url(),
            token_provider=celine_token_provider(),
        )
    return _client


def reset_client() -> None:
    """Drop the cached client. For tests, and for a settings change at boot."""
    global _client
    _client = None


def provisioning_url() -> str:
    """Where the provisioning service is, on the internal network.

    There is no default and there must not be a public one. The service holds
    realm-wide Keycloak administration and is safe to hold it only because
    nothing outside the network can reach it, so a value here is a deployment's
    own internal address — ``http://provisioning:8010`` under compose — and a
    default naming somebody's hostname would be wrong on every other checkout.
    """
    url = settings.provisioning_url.strip().rstrip("/")
    if not url:
        raise ConfigurationError(
            "PROVISIONING_URL is required to give a participant a login: this "
            "service holds no Keycloak grant and provisions accounts by calling "
            "celine-policies' provisioning service"
        )
    return url


def provisioning_enabled() -> bool:
    """Whether approval provisions a login at all.

    Gated on the URL having a value rather than on a flag of its own — the shape
    ``REC_REGISTRY_URL`` and ``DS_CONNECTOR_URL`` already have here. A
    deployment that onboards participants and gives them no login is supported;
    a flag saying it does so while no address is configured is not a
    configuration, it is a contradiction.
    """
    return bool(settings.provisioning_url.strip())


async def participant_community(rec_slug: str) -> str | None:
    """The community alias a participant is provisioned into, or ``None``.

    The REC manifest's ``rec_registry.community`` — the same value
    :mod:`~celine.onboarding.services.rec_registry` registers the member under.
    One alias from one place, because the provisioning service files the account
    into *that* community's Keycloak organization and the sweep that later
    checks the filing reads the community's own id from the registry export. Two
    answers to "which community" would put a participant in an organization
    nothing looks at.

    ``None`` when the REC declares no ``rec_registry`` block, and that is why a
    login cannot be provisioned for it: see :func:`provision_participant`.

    Async because the manifest cache has a TTL and is refreshed first, exactly as
    ``rec_registry`` does before reading the same block: a stale cache would key
    the account on a community the REC has stopped declaring, and the Keycloak
    organization it lands in is not something a later call can move it out of.
    """
    from celine.onboarding.services import template_service

    await template_service.ensure_fresh()
    binding = template_service.rec_registry_binding(rec_slug)
    return binding.community if binding.enabled else None


async def login_is_provisioned(rec_slug: str) -> bool:
    """Whether approving somebody in this REC gives them a login, and so an invitation.

    The two conditions step 1 checks before it calls anything, answered for a
    REC rather than a submission, so the wizard can say what approval will do
    before there is anything to approve.
    """
    return provisioning_enabled() and await participant_community(rec_slug) is not None


def participant_username(submission: Submission) -> str | None:
    """The username a *new* account would get for this submission, or ``None``.

    A proxy, not an observation. The provisioning service names an account it
    creates after the normalised address — and reads the name back off an
    account that already existed, which may authenticate under a convention
    nobody here chose. So anything running after provisioning in the same pass
    must use the ``username`` it returned instead of this.

    It survives as the fallback for a step that runs *without* provisioning in
    the same pass — a retry of registry registration on its own, where there is
    no returned value to use. See
    :func:`~celine.onboarding.services.rec_registry.member_user_id`.
    """
    email = (getattr(submission, "email", "") or "").strip().lower()
    return email or None


def keycloak_realm() -> str:
    """The realm the participant's account lives in.

    Nothing here administers that realm any more, and this is not an
    administration setting: the dataspace step tells the identity registry
    *where to find* the account, and the answer has to match where the
    provisioning service put it. Both are the realm ``OIDC_BASE_URL`` issues
    from — one realm, one issuer, and this service's token and the operator
    tokens it verifies all come from it. ``DATASPACE_KEYCLOAK_REALM`` overrides
    it for the deployment whose issuer URL names no realm.
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


_LOCALES: frozenset[str] = frozenset(get_args(ParticipantLocale))


def participant_locale(submission: Submission) -> str | None:
    """The language the invitation email is written in, or ``None`` for the realm's.

    The submission's own ``locale`` first — the language the person last used in
    the wizard — then the REC manifest's ``locale``, then nothing, which leaves
    Keycloak on the realm default.

    **Both are narrowed to ``it|en|es``, and anything else counts as absent.** The
    manifest's ``locale`` is free text, and the provisioning service answers
    ``422`` for any other value (the SDK raises before sending). Step 1 fails
    closed, so a manifest saying ``it-IT`` would block every approval in that
    community over a language tag — where sending nothing gives the same email.

    Not ``data_sharing_consent_locale``: that is evidence of the language a
    consent text was shown in, and it is empty for everyone who declined it.
    """
    if getattr(submission, "locale", None) in _LOCALES:
        return submission.locale

    from celine.onboarding.services import template_service

    try:
        manifest = template_service.load_manifest(submission.rec_slug)
    except KeyError:
        return None
    manifest_locale = manifest.get("locale")
    return manifest_locale if manifest_locale in _LOCALES else None


def _display_name(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _normalized_email(submission: Submission) -> str:
    email = (submission.email or "").strip().lower()
    if not email:
        raise ValueError("Cannot provision a login: approved submission has no email")
    return email


async def provision_participant(submission: Submission) -> ParticipantProvisionResult | None:
    """Ensure this participant has an account, filed in their REC.

    ``None`` means no login was provisioned and that is a supported state, for
    one of two reasons the caller reports separately:

    * no ``PROVISIONING_URL`` — this deployment onboards participants without
      giving them logins;
    * the REC declares no ``rec_registry`` block, so there is no community to
      key the account on.

    The second is worth stating plainly, because it is a narrowing. Revoking
    access resolves ``(community, key)`` **through the registry export**, so a
    login provisioned for a REC with no registry could never be revoked through
    this seam. Provisioning one anyway would trade a missing login for an
    unrevokable one, which is the worse of the two — and the community alias
    would have to be invented, creating a Keycloak organization that the
    reconcile sweep never looks at.

    Idempotent on ``(community, key)``: a retry finds the account the first call
    made and comes back with ``created`` false.

    **Always asks for an invitation**, including on a retry. Whether one is sent
    is the provisioning service's decision, because it is the one that can see
    credentials: it sends only to an account created in this call or one without
    a password, so a retry never emails somebody who has already set theirs. What
    it decided comes back as ``invitation``, and no outcome of it fails the call.
    """
    if not provisioning_enabled():
        return None

    community = await participant_community(submission.rec_slug)
    if community is None:
        return None

    from celine.sdk.provisioning import ProvisioningApiError

    email = _normalized_email(submission)
    try:
        account = await _get_client().ensure_participant(
            community,
            submission.ref,
            email=email,
            first_name=_display_name(submission.first_name),
            last_name=_display_name(submission.last_name),
            locale=participant_locale(submission),
            invite=True,
        )
    except ProvisioningApiError as exc:
        raise _refused("provisioning a login", exc, community=community) from exc

    # A plain `Enum` in the generated schema, so the value is read rather than
    # compared: `account.invitation == "sent"` is always false.
    invitation = getattr(account.invitation, "value", account.invitation)
    logger.info(
        "Provisioned %s/%s as '%s' (%s, invitation %s)",
        community,
        submission.ref,
        account.username,
        "created" if account.created else "already existed",
        invitation,
    )
    return ParticipantProvisionResult(
        user_id=account.user_id,
        username=account.username,
        created=account.created,
        invitation=invitation,
    )


async def disable_participant(submission: Submission) -> str:
    """Revoke this participant's access, without destroying anything.

    The account, its memberships and everything keyed on its uuid survive, and
    re-enabling is one call — what is being revoked is somebody's access to
    their own energy community. Erasure of their data is a separate act; see the
    purge path.

    **Runs before the registry member is deactivated, not after.** The
    provisioning service resolves ``(community, key)`` through the registry
    export and that export carries only ``active`` members, so a member
    deactivated first is a member this call cannot find — it would answer
    ``404`` and leave a revoked participant able to log in. The order is
    declared in ``enablement.REVOKE_ORDER``, which is why it is not simply the
    reverse of the provisioning order.
    """
    if not provisioning_enabled():
        return "no provisioning service is configured"

    community = await participant_community(submission.rec_slug)
    if community is None:
        return "this community declares no rec_registry binding"

    from celine.sdk.provisioning import ProvisioningApiError

    try:
        result = await _get_client().disable(community, submission.ref)
    except ProvisioningApiError as exc:
        code = exc.code
        if exc.status_code == 404 and code in NOTHING_TO_REVOKE:
            # No member under that key, or no account for one. Either way there
            # is nothing left to revoke, and refusing would leave the local
            # record claiming something that is no longer true — the same
            # reading `rec_registry.deactivate_member` gives a 404.
            logger.info(
                "Provisioning has no account for %s/%s (%s); nothing to disable",
                community,
                submission.ref,
                code,
            )
            return NOTHING_TO_REVOKE[code]
        # Every other 404 fails, `community_not_found` above all: the service
        # could not look the member up, so it cannot say whether a login exists,
        # and reading that as "nothing to revoke" would mark the row revoked while
        # the person can still sign in. A 404 with no code is not read as done
        # either — it is as likely a wrong `PROVISIONING_URL` as a missing member.
        raise _refused("revoking a login", exc, community=community) from exc

    return (
        f"disabled login {result.username}"
        if result.changed
        # Not a failure and must not be reported as one: the revocation was
        # already in force.
        else f"login {result.username} was already disabled"
    )


#: The `404` codes on a revocation that mean there is nothing to revoke, and what
#: the step row says for each. Only these two: the service resolved the lookup and
#: found no member, or a member with no account.
NOTHING_TO_REVOKE: dict[str, str] = {
    "member_not_found": "no member to disable",
    "account_not_found": "no account to disable",
}


def _refused(action: str, exc: Any, *, community: str | None = None) -> Exception:
    """Turn a provisioning refusal into something the right person can act on.

    Two audiences and one message would serve neither. A **misconfiguration**
    — no scope, no credential, no service — is a platform operator's to fix and
    reaches the REC operator's screen as an unactionable sentence, so it is
    raised as :class:`ConfigurationError`, which the enablement runner already
    knows to keep out of the step row and put in the log. Anything else is an
    outage or a bad submission: the status is what somebody can act on, and the
    service's own sentence goes to the log.

    Since the service's 1.2.0 contract every refusal carries a machine-readable
    ``code`` (``ProvisioningApiError.code``), and the branches read it rather
    than the message: ``community_not_found`` is the REC manifest binding a
    community the registry does not hold, so a configuration error; the ``502``
    codes are a dependency behind the service, so worth a retry.
    """
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    logger.warning("Provisioning refused %s (%s %s): %s", action, status, code, exc)

    if status in (401, 403):
        return ConfigurationError(
            f"The provisioning service refused {action} with {status}. Client "
            f"{settings.oidc_client_id!r} needs the scope "
            f"'provisioning.participants.write' and an audience mapper onto "
            f"svc-provisioning — celine-policies declares both in clients.yaml. "
            f"Check OIDC_CLIENT_SECRET too: a 401 is a credential the realm "
            f"would not read, a 403 one it read and found without the scope."
        )
    if code == "community_not_found":
        # The manifest binds a community the registry does not hold. Nothing the
        # reviewing operator can change, and nothing a retry fixes.
        return ConfigurationError(
            f"The provisioning service found no community {community!r} in the REC "
            f"registry while {action}. The REC manifest's rec_registry.community "
            f"must name a community the registry holds."
        )
    if code in _DEPENDENCY_FAILURES:
        return ValueError(f"{_DEPENDENCY_FAILURES[code]} while {action} ({status} {code}); retry")
    return ValueError(f"Provisioning refused {action} ({status}{f' {code}' if code else ''})")


#: The `502` codes: a dependency behind the provisioning service failed. Worth a
#: retry, which is what the step row is for, and said in words an operator can act
#: on. The service's own message stays in the log.
_DEPENDENCY_FAILURES: dict[str, str] = {
    "registry_unavailable": "The provisioning service could not reach the REC registry",
    "provisioning_failed": "Keycloak failed behind the provisioning service",
    "send_failed": "Keycloak could not send the email",
}
