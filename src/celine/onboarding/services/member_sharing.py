"""A member's own data-sharing decisions, read and changed as themselves.

The wizard that onboards a participant records an optional consent to share their
energy data into the dataspace. Nothing let them change it afterwards, which GDPR
Art. 7(3) requires to be as easy as giving it. This module is where withdrawal
lives.

**This is the first self-service surface in this service, and it is authorised
differently from everything else.** Every other route under `/api/admin` names a
`Capability` and is checked against the community in the path, so an operator is
allowed on their own REC and refused on somebody else's. Here there is no
capability and no operator: the member's own token is the entire authority, and
the credential presented to the connector is the member's own. A route that let
an admin decide on somebody's behalf would defeat the point of recording consent
at all — so if one is ever wanted, it is a different route with a different name,
not a parameter on these.

It moved here from `../celine-webapp`, which held a service account on
`identity-registry.resolve` and a live credential in request memory to do it.
Both are gone from there: a credential that never leaves the service that
resolved it cannot leak from the one that did not need it.

Three things worth knowing before changing this:

* **Never put a credential in a response**, and never cache one across requests.
  :class:`~celine.onboarding.services.dataspace_identity.SubjectCredential`
  redacts its own repr for the same reason.
* **Only consent-based offers get a control.** A contract-based offer is
  disclosed, not toggled: presenting a choice that does not exist is what
  invalidates consent. Refused here rather than left to the connector's 409, so
  the reason names the offer.
* **Offers come from the REC's allow-list**, not the whole published vocabulary.
  The BFF read the vocabulary directly and showed every offer the connector
  publishes, ignoring `consent.data_sharing.offers` — so a member could be shown,
  and could grant, an offer their community does not publish. Reusing
  `template_service.get_sharing_offers` is what fixes that, and it is why the
  merge belongs beside the thing that already resolves offers per REC.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from celine.sdk.auth import JwtUser

from celine.onboarding.config.settings import settings
from celine.onboarding.services import dataspace_identity, rec_registry, template_service

logger = logging.getLogger(__name__)

#: Statuses the connector uses for a decision that currently stands. More than
#: one because the vocabulary has moved and a stale name reads as "not granted",
#: which is the direction that silently under-reports what somebody agreed to.
_GRANTED = frozenset({"granted", "approved", "active"})


class SharingUnavailableError(RuntimeError):
    """The dataspace could not be reached, or is not configured."""


class SharingState(enum.StrEnum):
    """Why a member can or cannot decide anything, in one word.

    The states exist because collapsing them is what made the feature useless:
    a preregistered member reached the sharing screen and was told they had no
    dataspace identity, which was true and unactionable. Each of these has a
    different thing to say and a different thing to do next.
    """

    #: Offers listed, decisions merged, controls live.
    OK = "ok"
    #: This member's community does not take part in a dataspace, so there is
    #: nothing to decide and nothing to provision. **Not** the same as
    #: :attr:`NO_IDENTITY`: provisioning somebody whose community is not in the
    #: dataspace is worse than explaining why there is nothing to share.
    NO_DATASPACE = "no_dataspace"
    #: The community takes part; this member holds no credential yet. The state
    #: a preregistered member is in, and the one Phase 3 provisions out of.
    NO_IDENTITY = "no_identity"
    #: `/users/resolve` answered 409. Terminal for the member — retrying cannot
    #: clear it — and logged for an operator, who is the only one who can.
    IDENTITY_CONFLICT = "identity_conflict"
    #: This member is in more than one participating community, so "their
    #: offers" has no single answer. Refused rather than guessed; see
    #: :func:`resolve_member_rec`.
    AMBIGUOUS_COMMUNITY = "ambiguous_community"


class CannotDecideError(RuntimeError):
    """This member cannot decide anything, and :attr:`state` says why.

    Raised only by :func:`set_data_sharing`. The read path returns the state in
    its response instead, because "there is nothing to decide" is an answer a
    member is shown rather than an error.
    """

    def __init__(self, state: SharingState) -> None:
        super().__init__(str(state))
        self.state = state


@dataclass(frozen=True, slots=True)
class SharingView:
    """What a member is shown, and what the BFF passes straight through.

    ``has_identity`` is kept alongside ``state`` because the one consumer —
    `../celine-webapp` and the page built on it — reads that field, and a
    relocation must not make the frontend change. ``state`` is the additive half
    that says *why* it is false.
    """

    state: SharingState
    offers: list[dict[str, Any]] = field(default_factory=list)
    #: The member's own dataspace identity, as far as they need to know it:
    #: enough to quote to a REC manager who is looking them up, and nothing that
    #: authenticates as them. ``None`` unless :attr:`state` is ``OK``.
    identity: dict[str, Any] | None = None

    @property
    def has_identity(self) -> bool:
        return self.state is SharingState.OK


def _identity_of(credential: dataspace_identity.SubjectCredential) -> dict[str, Any]:
    """The four facts a member may be shown about their own credential.

    Built field by field, never by serialising the credential. `vc_jws` sits on
    the same object and **is** the member's authentication — the difference
    between this and `asdict()` is the whole guarantee, so it must stay a list
    somebody has to add to deliberately.

    The DID is here because a REC manager looking a member up in the registry
    needs it and the member has no other way to learn it. The dates are here
    because "my sharing stopped working" and "my credential expired last week"
    are the same event, and only one of them is visible to the person.
    """
    return {
        "did": credential.subject_id,
        "role": credential.role,
        "issued_at": credential.issued_at,
        "expires_at": credential.expires_at,
    }


def resolve_member_rec(user: JwtUser) -> str | None:
    """Which community this member belongs to, from their token alone.

    The join is the **Keycloak organization alias**, which this platform keeps as
    one identifier: the owner `id` in the deployment's owners.yaml, the alias in
    the identity registry, and a REC manifest's top-level `organization:`. So a
    member's `organization` claim resolves to a REC without a mapping table and
    without a database read.

    Deliberately not `dataspace.organization`. That would resolve only a
    community already in a dataspace, making "your REC does not take part"
    indistinguishable from "you are in no community I serve" — the distinction
    :class:`SharingState` exists to draw. `organization_for` falls back to the
    dataspace alias for a manifest that only declares one, so a community bound
    before the top-level key existed still resolves.

    Returns ``None`` when no REC matches, and raises :class:`ValueError` when
    more than one does — a member of two participating communities has no single
    set of offers, and picking one would show them another community's
    allow-list. Rare enough to be a deployment surprise rather than a supported
    shape, which is why it is refused loudly instead of resolved by a rule
    nobody has written.
    """
    aliases = user.organization_aliases

    candidates: list[str] = []
    for alias in aliases:
        for slug in template_service.recs_for_organization(alias):
            if slug not in candidates:
                candidates.append(slug)

    if not candidates:
        return None
    if len(candidates) > 1:
        raise ValueError(f"member belongs to more than one community: {', '.join(candidates)}")
    return candidates[0]


def keycloak_realm_of(user: JwtUser) -> str | None:
    """The realm that actually authenticated this member, from their issuer.

    Deliberately **not** `DATASPACE_KEYCLOAK_REALM`, which defaults to
    `dataspaces` and names the dedicated realm the funnel *creates* users in. A
    preregistered member already has an account, in whichever realm they log into
    — celine's — and binding their DID to the wrong realm would produce a mapping
    the connector cannot resolve them through. The issuer is the only thing that
    knows which realm a token came from.
    """
    issuer = (user.iss or "").rstrip("/")
    marker = "/realms/"
    if marker not in issuer:
        return None
    realm = issuer.rsplit(marker, 1)[1].strip("/")
    return realm or None


async def _provision_from_preregistration(
    access: dataspace_identity.RegistryAccess,
    user: JwtUser,
    binding: template_service.DataspaceBinding,
    subject_id: str,
) -> dataspace_identity.SubjectCredential | None:
    """Make this member a dataspace subject, on the strength of preregistration.

    **The second door.** The REC screened these members offline and installed
    their meters on signature; there is no submission and there never will be, so
    the funnel's entrypoint cannot reach them. The authority is that
    preregistration, and the credential records the same assurance the funnel's
    does — the operator's answer on 2026-09-06 was that the two *are* the same
    check, performed in different places.

    Called only where :func:`_resolve` has already established that this
    member's community takes part and that they hold no presentable credential.
    **That is the idempotency guard**, and it matters: ds reuses the subject DID
    but mints a fresh credential and burns a revocation slot on every issuance,
    so a page that provisioned on every visit would do so once per visit.

    ``subject_id`` comes from the caller's own resolve rather than being read
    again here: the call that established there was no credential established
    the identifier too.

    Returns the credential the member can now act with, or ``None`` if issuance
    produced nothing presentable — which is not an error the member can act on
    either, and leaves them where they were.
    """
    realm = keycloak_realm_of(user)
    if not realm:
        # Without a realm the Keycloak sync would bind the DID to nothing, and
        # the connector resolves a subject to a data-plane identity through
        # exactly that mapping. Better no credential than an unusable one.
        logger.error("Cannot provision member %s: no realm in issuer %r", user.sub, user.iss)
        return None

    identity = await dataspace_identity.provision_subject(
        access,
        dataspace_identity.SubjectFacts(
            subject_id=subject_id,
            role=settings.dataspace_user_role,
            email=user.email,
            keycloak_user_id=user.sub,
            keycloak_realm=realm,
            # `preferred_username` is what the data plane joins on — the same
            # value the funnel writes into `Member.user_id`. The registry falls
            # back to the email without it, which is right only while the two
            # agree.
            keycloak_username=user.preferred_username,
            verified_by=binding.organization_did or None,
            verification_method=dataspace_identity.VERIFICATION_METHOD,
            allowed_actions=tuple(
                a.strip() for a in settings.dataspace_allowed_actions.split(",") if a.strip()
            ),
            ttl_days=settings.dataspace_vc_ttl_days,
        ),
        binding,
    )
    logger.info(
        "Provisioned dataspace identity %s for member %s on preregistration",
        identity.did,
        user.sub,
    )

    # Re-resolve rather than build a credential from the issuance response: it
    # returns the DID, the credential id and a timestamp, and no `vc_jws` — which
    # is the one thing the member needs in order to act.
    return await dataspace_identity.resolve_subject_credential(access, email=user.email)


async def _resolve(
    user: JwtUser, *, provision: bool = True
) -> tuple[SharingState, str | None, Any]:
    """Resolve community and credential, returning the state that stopped it.

    Returns ``(state, rec_slug, credential)``. Only ``SharingState.OK`` carries
    both; every other state is terminal for this request and carries whatever was
    established before it stopped.

    ``provision`` is False on the history route. A member with no identity has no
    history, and minting a credential to discover that is both gratuitous and a
    second writer racing the read that already provisions.
    """
    if not settings.dataspace_enabled:
        return SharingState.NO_DATASPACE, None, None

    await template_service.ensure_fresh()

    try:
        rec_slug = resolve_member_rec(user)
    except ValueError as exc:
        logger.error("Cannot resolve a single community for member %s: %s", user.sub, exc)
        return SharingState.AMBIGUOUS_COMMUNITY, None, None

    if rec_slug is None:
        # Not an error and not worth an error log: a platform user who is in no
        # community this deployment serves is an ordinary thing to be.
        logger.debug("Member %s matches no community served here", user.sub)
        return SharingState.NO_DATASPACE, None, None

    binding = template_service.dataspace_binding(rec_slug)
    if not binding.enabled:
        return SharingState.NO_DATASPACE, rec_slug, None

    if not user.email:
        # The dataspace identity is keyed on the email the participant was
        # onboarded with. Without one there is nothing to resolve, and that is a
        # broken session rather than a member without a credential.
        raise SharingUnavailableError("No email address on the current session")

    try:
        access = await dataspace_identity.registry_access()
        resolved, credential = await dataspace_identity.resolve_subject_and_credential(
            access, email=user.email
        )
    except dataspace_identity.SubjectIdentifierConflictError:
        # Already logged at error level with the identifiers, which is the only
        # thing that brings an operator to it.
        return SharingState.IDENTITY_CONFLICT, rec_slug, None
    except (httpx.HTTPError, ValueError) as exc:
        raise SharingUnavailableError(f"Identity registry unavailable: {exc}") from exc

    if credential is None:
        if not provision:
            return SharingState.NO_IDENTITY, rec_slug, None
        try:
            credential = await _provision_from_preregistration(
                access, user, binding, resolved.subject_id
            )
        except dataspace_identity.SubjectIdentifierConflictError:
            return SharingState.IDENTITY_CONFLICT, rec_slug, None
        except (httpx.HTTPError, ValueError) as exc:
            raise SharingUnavailableError(f"Could not provision an identity: {exc}") from exc
        if credential is None:
            return SharingState.NO_IDENTITY, rec_slug, None

    return SharingState.OK, rec_slug, credential


async def _list_decisions(credential: dataspace_identity.SubjectCredential) -> list[dict[str, Any]]:
    """The member's current decisions, as themselves."""
    base = (settings.ds_connector_url or "").rstrip("/")
    if not base:
        raise SharingUnavailableError("DS_CONNECTOR_URL is not configured")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{base}/consent/my/shares", headers=credential.headers)
    except httpx.HTTPError as exc:
        raise SharingUnavailableError(f"Connector unreachable: {exc}") from exc

    if resp.status_code >= 400:
        raise SharingUnavailableError(f"Connector answered {resp.status_code}")

    body = resp.json()
    return body if isinstance(body, list) else body.get("items", [])


def _merge(offers: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One offer per row, with this member's decision on it.

    The offer is the published projection — the same facts the person was shown —
    and the decision is the connector's. Neither is copied or cached: two records
    of one fact is how the thing displayed and the thing enforced drift apart.
    """
    standing = {
        d.get("offer_id"): d for d in decisions if d.get("offer_id") and d.get("status") in _GRANTED
    }

    merged: list[dict[str, Any]] = []
    for offer in offers:
        decision = standing.get(offer.get("id"))
        merged.append(
            {
                **offer,
                "granted": decision is not None,
                # Whether this offer is the member's to decide. A contract-based
                # offer is disclosed and not toggled; rendering a control for it
                # would present a choice that does not exist.
                "can_decide": bool(offer.get("requires_consent")),
                # Codes and hashes only — the record of what was shown when the
                # decision was made, never anything about the person.
                "evidence": (decision or {}).get("legal_basis"),
                "decided_at": (decision or {}).get("decided_at"),
            }
        )
    return merged


async def get_data_sharing(user: JwtUser) -> SharingView:
    """Every offer this member's community publishes, and their decision on it."""
    state, rec_slug, credential = await _resolve(user)
    if state is not SharingState.OK:
        return SharingView(state=state)

    assert rec_slug is not None  # noqa: S101 - narrowing; OK implies both

    # **The join that makes a consent count**, reconciled on every read.
    #
    # The POD export asks the connector who consented — in DIDs — and the
    # registry what they hold, and `Member.did` is the only thing connecting the
    # two. Enablement writes it for a member the funnel approved; nothing wrote
    # it for a member provisioned here, so their consent would be recorded, be
    # reported in the audience, and produce no rows. Silently: a missing join
    # looks exactly like a person who holds no supply point.
    #
    # Done here rather than only after provisioning so it is self-healing — a
    # write that failed once, or a member provisioned before this existed, is
    # fixed by them opening the page. It never raises and never changes what the
    # member sees; a row that already holds the DID costs one lookup.
    if user.preferred_username:
        detail = await rec_registry.ensure_member_did(
            rec_slug, user_id=user.preferred_username, did=credential.subject_id
        )
        logger.debug("Registry DID reconciliation for %s: %s", user.sub, detail)
    else:
        # `Member.user_id` holds a Keycloak *username*, and it is the only key
        # the registry can be searched by here. Without one there is nothing to
        # look the member up with.
        logger.warning(
            "Member %s has no preferred_username, so their dataspace DID cannot be "
            "written to the registry and their supply points will not be exported",
            user.sub,
        )

    try:
        offers = await template_service.get_sharing_offers(rec_slug)
    except template_service.SharingOffersUnavailableError as exc:
        # Fail closed, exactly as the wizard does. A silent empty list is
        # indistinguishable from "this community shares nothing", and a member
        # shown that would reasonably conclude they had nothing to withdraw.
        raise SharingUnavailableError(str(exc)) from exc

    return SharingView(
        state=state,
        offers=_merge(offers, await _list_decisions(credential)),
        identity=_identity_of(credential),
    )


async def set_data_sharing(user: JwtUser, offer_id: str, *, enabled: bool) -> SharingView:
    """Grant or withdraw one offer, as the member.

    No evidence record is sent. The connector derives it from the resolved offer
    server-side, which is what stops this service recording consent to something
    other than what it displayed — the asymmetry with the onboarding wizard,
    which renders its own text and therefore has to prove what it showed.
    """
    state, rec_slug, credential = await _resolve(user)
    if state is not SharingState.OK:
        raise CannotDecideError(state)

    assert rec_slug is not None  # noqa: S101 - narrowing; OK implies both

    # Refused here rather than left to the connector's 409, twice over: an offer
    # this REC does not publish is not this member's to grant, and a
    # contract-based one is not anybody's. Both messages can name the offer;
    # a 409 from two hops away cannot.
    try:
        offer = await template_service.get_sharing_offer(rec_slug, offer_id)
    except template_service.SharingOffersUnavailableError as exc:
        raise SharingUnavailableError(str(exc)) from exc

    if not offer.get("requires_consent"):
        raise ValueError(
            f"Offer {offer_id!r} is not consent-based — it is disclosed under a "
            "contract and cannot be toggled."
        )

    base = (settings.ds_connector_url or "").rstrip("/")
    if not base:
        raise SharingUnavailableError("DS_CONNECTOR_URL is not configured")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{base}/consent/my/shares",
                json={"offer_id": offer_id, "enabled": enabled},
                headers=credential.headers,
            )
    except httpx.HTTPError as exc:
        raise SharingUnavailableError(f"Connector unreachable: {exc}") from exc

    if resp.status_code == 409:
        # The check above should have caught this; reaching it means the
        # published vocabulary and the connector disagree about the offer.
        raise ValueError(
            f"The connector refused offer {offer_id!r} as not consent-based, "
            "though the published vocabulary says it is."
        )
    if resp.status_code >= 400:
        raise SharingUnavailableError(f"Connector refused the change ({resp.status_code})")

    return await get_data_sharing(user)


async def get_history(user: JwtUser) -> tuple[SharingState, list[dict[str, Any]]]:
    """What has happened with this member's data, from their own record.

    Served by provenance under the member's own credential, so it is their
    history rather than a view this service assembles.

    **`DS_PROVENANCE_URL` is read-only here, and only here.** The setting was
    removed from this service when `DataDisclosed` moved to the connector's
    `POST /admin/disclosure`, which computes the consent-snapshot hash a
    disclosure record requires. Nothing about that changed: this is the member's
    Art. 15 read of events already recorded, and no disclosure is ever written
    through it. Unset returns an empty list rather than failing — the decisions
    stand without their history, and failing here would make the whole page
    unusable for a detail.
    """
    state, _, credential = await _resolve(user, provision=False)
    if state is not SharingState.OK:
        return state, []

    base = (settings.ds_provenance_url or "").rstrip("/")
    if not base:
        return state, []

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{base}/prov/my/events", headers=credential.headers)
    except httpx.HTTPError as exc:
        logger.warning("Provenance unreachable: %s", exc)
        return state, []

    if resp.status_code >= 400:
        logger.warning("Provenance answered %s", resp.status_code)
        return state, []

    body = resp.json()
    events = body.get("@graph", body) if isinstance(body, dict) else body
    return state, events if isinstance(events, list) else []
