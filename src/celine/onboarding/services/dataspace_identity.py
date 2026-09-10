from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from celine.sdk.auth import OidcClientCredentialsProvider

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission
from celine.onboarding.services import template_service
from celine.onboarding.services.service_auth import service_token_provider

logger = logging.getLogger(__name__)

_SAFE_SUBJECT = re.compile(r"^[A-Za-z0-9._+-]{1,128}$")

_token_provider: OidcClientCredentialsProvider | None = None

_KC_SYNC_MAX_RETRIES = 3


def _get_token_provider() -> OidcClientCredentialsProvider:
    """This service's own service account, built once in `services.service_auth`.

    The module-level handle stays here because it is what a test substitutes.
    """
    global _token_provider
    if _token_provider is None:
        _token_provider = service_token_provider()
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
    return datetime.now(UTC)


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
                "No dataspace binding for REC %r; the disclosure will not name a disclosing agent",
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
        resp = await client.post(f"{base_url}/admin/disclosure", json=payload, headers=headers)

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
    did: str | None = None
    """The owner's dataspace identifier, when the registry published one.

    Carried because ``/owners/resolve`` already returns it and the POD export
    needs exactly this mapping: a sharing offer names its controller by *alias*,
    and the consent plane is keyed by *DID*. The registry is the only place that
    mapping exists, so reading it here is what stops a second one being invented
    somewhere else.

    None when the owner was not found, when the registry did not report one, or
    when the owner has not been onboarded into the dataspace yet.
    """


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
        body = resp.json()
        status = str(body.get("status") or "").strip() or None
        did = str(body.get("did") or "").strip() or None
    except ValueError:
        # Found, but the body was not readable. Do not invent a status: an
        # absent one must not read as "not verified" and refuse boot.
        status = None
        did = None
    return OwnerCheck(found=True, status=status, did=did)


async def resolve_consumer_did(controller_alias: str) -> str:
    """The dataspace identifier of the party a sharing offer names as controller.

    A sharing offer names its controller by **alias** — ``grid-operator``,
    ``example-org`` — and the connector's consent plane is keyed by **DID**. The
    identity registry holds the only mapping between the two, and it is already
    reachable from here: this is the same ``/owners/resolve`` call
    :func:`check_organization` makes at boot, read for a different field.

    **The alias comes from the offer, never from the community.** The person
    consented to disclosure to the controller *that offer names*; taking the
    recipient from anywhere else — a manifest binding, the REC's grid operator —
    can hand data to a party the offer does not name, which is a disclosure
    against a consent nobody gave.

    Raises ``ValueError`` when the mapping cannot be made, because every reason
    it cannot is a deployment configuration an operator can fix, and refusing is
    the only safe answer: the connector would accept a wrong-but-plausible DID
    and return an audience for it.
    """
    check = await check_organization(controller_alias)
    if check.found is None:
        raise RuntimeError(
            f"The identity registry could not be reached to resolve controller "
            f"{controller_alias!r}, so the recipient of this disclosure is "
            "unknown and it must not proceed."
        )
    if not check.found:
        raise ValueError(
            f"The sharing offer names controller {controller_alias!r}, which the "
            "identity registry does not know. Register the owner before "
            "exporting under this offer."
        )
    if not check.did:
        raise ValueError(
            f"Controller {controller_alias!r} is registered but holds no "
            "dataspace identifier, so the consent plane has no key to answer "
            "for it. Onboard the owner into the dataspace — mint its DID and "
            "record it on the owner — before exporting under this offer."
        )
    return check.did


@dataclass(frozen=True, slots=True)
class OfferAudience:
    """Who currently consents to one offer, for one recipient.

    ``subject_ids`` are dataspace DIDs, which is what makes this joinable
    against ``Submission.dataspace_did``.
    """

    dataset_id: str
    subject_ids: frozenset[str]
    subject_count: int


async def get_offer_audience(offer_id: str, consumer_id: str) -> OfferAudience:
    """Ask the connector who currently consents to *offer_id* for *consumer_id*.

    The read counterpart to :func:`provision_user_shares`, and the reason the
    POD export can stop reading a form. A ``Submission`` records what somebody
    agreed to on one afternoon; the connector holds the decision as it stands
    now, including one made or withdrawn in the participant webapp afterwards.

    **The purpose and controller role are not sent, and must not be.** The
    connector stamps them from the offer, the same way
    ``POST /consent/admin/shares`` and ``POST /admin/disclosure`` already do. A
    caller that cannot supply a purpose cannot omit one, which is what makes the
    under-specification that answers "nobody" on the connector's internal check
    unreachable from here.

    **``consumer_id`` is required and is never the wildcard.** The standing
    rows this service writes are wildcard-scoped, and a per-party opt-out beats
    the standing wildcard. Asking as the wildcard would read only the standing
    rows and return people who have specifically opted out of *this* recipient —
    a disclosure against a withdrawn consent, which is the thing the whole
    change exists to prevent. The connector refuses it; naming the recipient is
    this caller's part of that.

    **One dataset, or nothing.** The response carries one subject set per
    dataset the offer resolves to and deliberately does not flatten them. A
    single CSV cannot honestly carry two audiences: reading the first element
    would be right today and silently wrong the day a second dataset declares
    the offer. So more than one is refused here rather than merged.
    """
    if not settings.ds_connector_url:
        raise RuntimeError(
            "DS_CONNECTOR_URL is not configured, so the connector cannot be asked who consents."
        )

    base_url = settings.ds_connector_url.rstrip("/")
    headers = await _auth_headers()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{base_url}/consent/admin/shares",
                params={"offer_id": offer_id, "consumer_id": consumer_id},
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"The connector could not be reached to read the audience for offer "
            f"{offer_id!r}, so who consents is unknown and the export must not "
            f"proceed: {exc}"
        ) from exc

    if resp.status_code in (409, 422):
        # The connector's own two answers about the offer: unknown, or not
        # consent-based. Both are the caller naming the wrong offer, and both
        # are fixable without touching the deployment.
        raise ValueError(
            f"The connector refused to report an audience for offer {offer_id!r} "
            f"({resp.status_code}): {resp.text}"
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"The connector answered {resp.status_code} reading the audience for "
            f"offer {offer_id!r}, so who consents is unknown and the export must "
            f"not proceed: {resp.text}"
        )

    try:
        body = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"The connector's audience for offer {offer_id!r} was not readable, "
            "so who consents is unknown and the export must not proceed."
        ) from exc

    datasets = body.get("datasets") or []
    if not datasets:
        # Distinct from "nobody consents": the connector refuses an offer that
        # resolves to no dataset with a 422, so an empty list here means the
        # response was not the shape this caller was written against.
        raise RuntimeError(
            f"The connector reported no dataset for offer {offer_id!r}; refusing "
            "to export against an audience that describes nothing."
        )
    if len(datasets) > 1:
        raise ValueError(
            f"Offer {offer_id!r} resolves to {len(datasets)} datasets "
            f"({', '.join(str(d.get('dataset_id')) for d in datasets)}), and one "
            "file cannot carry two audiences without saying which row came from "
            "which. Export per dataset, or narrow the offer."
        )

    entry = datasets[0]
    subject_ids = frozenset(str(s) for s in (entry.get("subject_ids") or []))
    return OfferAudience(
        dataset_id=str(entry.get("dataset_id") or ""),
        subject_ids=subject_ids,
        # The connector's own count, kept beside the set it was taken from
        # rather than recomputed: they agreeing is worth being able to assert,
        # and they are two different claims.
        subject_count=int(entry.get("subject_count") or 0),
    )


# **How the person was checked, and by whom.** Both doors — the onboarding funnel
# and the participant wizard — record the same value, because the operator's answer
# on 2026-09-06 was that they *are* the same check: "same checks, but one outside
# onboarding, the other is the user doing it in onboarding." The pair records the
# assurance, not the entrypoint. Where the check happened is not a property of the
# person's identity and does not belong in their credential.
#
# A constant rather than a setting, deliberately. A deployment that could edit this
# could make the credential claim an assurance level nobody established, which is
# the exact failure `verified_by` exists to prevent (ds `D-53`).
VERIFICATION_METHOD = "submission-review"


@dataclass(frozen=True, slots=True)
class RegistryAccess:
    """One registry, one token, for a whole provisioning flow.

    Resolve, check and issue are three calls against the same instance, and
    before this they each fetched their own token and re-derived the same base
    URL. One handle means one token fetch per flow and makes it impossible for
    two calls in one flow to address different registries — which after
    `D-49`/`DID-11` is a mistake the topology now permits.

    Every route this service calls is served by the **anchor**: ds's
    `identity_registry/roles.py` marks `/credentials/check`, `/admin/*` and
    `/memberships` `ANCHOR_ONLY`, and `/users/*` `BOTH`. The holder-side routes
    (`/credentials/{did}/presentations/query`, `/sts`) are not called from here.
    So one URL is correct, and this type is where that stops being an accident.
    """

    base_url: str
    headers: dict[str, str]


async def registry_access() -> RegistryAccess:
    """The anchor identity-registry, authenticated as this service."""
    if not settings.identity_registry_url:
        raise ValueError("IDENTITY_REGISTRY_URL is required when dataspace VC is enabled")
    return RegistryAccess(
        base_url=settings.identity_registry_url.rstrip("/"),
        headers=await _auth_headers(),
    )


class SubjectIdentifierConflictError(ValueError):
    """`/users/resolve` answered 409: the identifier matches somebody else's row.

    ds quarantines rather than reconciles, because "account re-created" and
    "address recycled to a different human" are indistinguishable from there —
    so this is an operator's decision and a member cannot act on it. Raised as
    its own type precisely so a member-facing caller can say *"we cannot confirm
    your dataspace identity; your REC manager has been notified"* and offer no
    retry, instead of the "registry unavailable, try later" that every non-200
    used to read as and that a member could retry forever without the state
    changing.
    """


@dataclass(frozen=True, slots=True)
class ResolvedSubject:
    """What the registry knows about a person before anything is issued.

    ``did`` is ``None`` when no mapping exists yet — which with ``derive=true`` is
    not an error but the ordinary first-time answer, carrying a freshly derived
    ``subject_id`` and nothing else.
    """

    subject_id: str
    did: str | None = None


@dataclass(frozen=True, slots=True)
class SubjectFacts:
    """Who is becoming a dataspace subject, and on whose authority.

    The argument that replaced ``Submission`` on this path. Provisioning is a
    **function, not a stage**: a preregistered member — screened offline, meter
    installed on signature — has no submission and never will, and keying it on
    one made the funnel the only door. Whoever holds an authenticated member with
    no dataspace identity fills these in.
    """

    subject_id: str
    role: str
    email: str | None = None
    keycloak_user_id: str | None = None
    keycloak_realm: str | None = None
    keycloak_username: str | None = None
    verified_by: str | None = None
    verification_method: str | None = None
    allowed_actions: tuple[str, ...] = ()
    ttl_days: int | None = None


@dataclass(frozen=True, slots=True)
class SubjectIdentity:
    """The dataspace identity a person holds once this function returns."""

    did: str
    credential_id: str
    issued_at: datetime


async def resolve_subject(
    access: RegistryAccess,
    *,
    email: str | None = None,
    keycloak_realm: str | None = None,
    keycloak_user_id: str | None = None,
    username: str | None = None,
) -> ResolvedSubject:
    """Ask the registry who this person is, deriving an id if they are new.

    The IR is the sole authority on the email→``subject_id`` mapping: it either
    returns an existing one or derives a new one keyed by its own secret. A
    failure here is fatal — the credential issuance that follows requires the
    same service, so swallowing the error would only delay it.

    ``derive=true`` **requires an email** (422 otherwise), because the derivation
    is seeded by the email and nothing else; deriving from a username would mint
    a second identity for somebody who may already have one.

    Returns the DID too when a mapping exists. The response also carries the
    person's ``vc_jws``; it is deliberately **not** read here. This function's job
    is identification, and a credential that is never lifted out of the response
    cannot leak from a caller that did not need it — see
    :func:`resolve_subject_credential` for the path that does need it.
    """
    params: dict[str, str] = {"derive": "true"}
    if email:
        params["email"] = email
    if username:
        params["username"] = username
    if keycloak_realm and keycloak_user_id:
        params["realm"] = keycloak_realm
        params["user_id"] = keycloak_user_id

    body = await _resolve_raw_with_params(access, params)
    return ResolvedSubject(subject_id=body["subject_id"], did=body.get("did") or None)


async def _resolve_raw(access: RegistryAccess, *, email: str) -> dict[str, Any]:
    """:func:`resolve_subject`'s call, returning the body rather than a summary.

    Separate because :func:`resolve_subject_credential` needs the ``credentials``
    list, and :func:`resolve_subject` deliberately does not read it — an
    identification function that lifted a live credential out of the response
    would be exactly what moving this away from the BFF was meant to stop.
    """
    return await _resolve_raw_with_params(access, {"email": email, "derive": "true"})


async def _resolve_raw_with_params(
    access: RegistryAccess, params: dict[str, str]
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{access.base_url}/users/resolve", params=params, headers=access.headers
        )

    if resp.status_code == 409:
        # Loud, and with the identifiers, because nobody else will look. The
        # member gets a terminal explanation and no retry; this line is the only
        # thing that tells an operator there is something to decide.
        logger.error(
            "Identity conflict resolving subject (%s): %s",
            ", ".join(f"{k}={v!r}" for k, v in params.items() if k != "derive"),
            resp.text,
        )
        raise SubjectIdentifierConflictError(
            "The identity registry holds a mapping for this identifier under a "
            "different Keycloak user. Only an operator can resolve it; retrying "
            "will not."
        )

    if resp.status_code == 200:
        body = resp.json()
        if body.get("subject_id"):
            return body

    raise ValueError(f"Subject id derivation failed: identity registry returned {resp.status_code}")


@dataclass(frozen=True, slots=True)
class SubjectCredential:
    """What a member needs in order to act on their own consent.

    The connector authenticates a data subject **by verifiable credential**
    (`X-Subject-Id` + `X-User-VC`), never by a service token — a service account
    that could grant consent on somebody's behalf would defeat the point of
    recording it. So the only thing done for the member is *resolving* which
    credential is theirs; every decision is then presented with it.

    **Never put one of these in a response, and never cache one across
    requests.** It authenticates as that person.
    """

    subject_id: str
    vc_jws: str
    #: What a member can be shown, and quote to a REC manager. Read from the same
    #: registry entry as ``vc_jws`` because they describe *that* credential — the
    #: one being presented — and a role taken from a different entry would
    #: describe a capacity the member is not acting in.
    role: str | None = None
    issued_at: str | None = None
    expires_at: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Subject-Id": self.subject_id, "X-User-VC": self.vc_jws}

    def __repr__(self) -> str:
        """Never render the credential.

        A dataclass repr would print `vc_jws` in full, and this object reaches
        exception messages, log records and test failure output. One of those
        eventually gets shipped somewhere.
        """
        return f"SubjectCredential(subject_id={self.subject_id!r}, vc_jws=<redacted>)"


async def resolve_subject_and_credential(
    access: RegistryAccess,
    *,
    email: str,
) -> tuple[ResolvedSubject, SubjectCredential | None]:
    """Who this person is, and the credential they can act with, in one call.

    Both answers come out of the same ``/users/resolve`` body, and a caller that
    may have to provision needs both: the ``subject_id`` to issue against, and
    the credential to know whether to. Asking twice was a real cost — the second
    read told nobody anything the first had not already said.

    The credential is ``None`` when they hold none, which is an ordinary answer:
    a participant enabled before the dataspace existed has no credential, and
    neither does a preregistered member who has not been provisioned yet.

    **Selected by role, not by recency.** One human legitimately holds several
    credentials — a data subject about their own consumption, a consumer user
    acting for somebody else — and the registry returns them all. The singular
    ``role`` / ``vc_jws`` fields are the most recently issued one, which for such
    a person is the wrong credential; presenting it to the consent API would
    authenticate them in a capacity they are not acting in. The singular fields
    are a fallback, and only when they name the right role.

    Raises :class:`SubjectIdentifierConflictError` on a 409, which a
    member-facing caller must not present as a retryable failure.
    """
    body = await _resolve_raw(access, email=email)
    resolved = ResolvedSubject(subject_id=body["subject_id"], did=body.get("did") or None)

    subject_id = body.get("did") or body.get("subject_did")
    if not subject_id:
        return resolved, None

    for credential in body.get("credentials") or []:
        if credential.get("role") == settings.dataspace_user_role and credential.get("vc_jws"):
            return resolved, _as_subject_credential(subject_id, credential)

    if body.get("role") == settings.dataspace_user_role and body.get("vc_jws"):
        return resolved, _as_subject_credential(subject_id, body)

    return resolved, None


async def resolve_subject_credential(
    access: RegistryAccess,
    *,
    email: str,
) -> SubjectCredential | None:
    """The member's own credential, for a caller that needs nothing else."""
    _, credential = await resolve_subject_and_credential(access, email=email)
    return credential


def _as_subject_credential(subject_id: str, entry: dict[str, Any]) -> SubjectCredential:
    def _text(value: Any) -> str | None:
        return str(value) if value else None

    return SubjectCredential(
        subject_id=subject_id,
        vc_jws=entry["vc_jws"],
        role=_text(entry.get("role")),
        issued_at=_text(entry.get("issued_at")),
        expires_at=_text(entry.get("expires_at")),
    )


async def provision_subject(
    access: RegistryAccess,
    facts: SubjectFacts,
    binding: template_service.DataspaceBinding,
) -> SubjectIdentity:
    """Mint a person's dataspace identity, and tell Keycloak who they are.

    **The whole of provisioning, taking facts rather than a database row.** Both
    doors converge here and differ only in who established that the person may
    become a subject — a REC manager approving a submission, or the
    preregistration the REC did offline. The credential records the assurance
    either way; see :data:`VERIFICATION_METHOD` for why it does not record which
    door.

    **This function always calls, and the registry decides whether that mints.**
    A repeat call for somebody who already holds an active credential *in the
    same role* returns the one they hold — same ``credentialId``, no second
    status-list index — and re-delivers it to their custodian. A different role
    mints, because roles are additive: one person is a data subject about their
    own consumption and may be a consumer user acting for somebody else. The
    subject DID is settled by the first call and reused by every later one.

    **That was not always true, and both doors were built when it was not**: a
    repeat call used to mint a fresh credential and spend a status-list index,
    which is never recovered. Both guards are still right, for reasons that have
    moved:

    * The **funnel** does not ask. A manager has just approved this submission,
      and the row needs a ``dataspace_vc_id`` of its own to stay revocable — which
      it gets whether the registry minted or matched, since the response names the
      credential either way.
    * The **wizard** asks by calling :func:`resolve_subject_credential` first and
      provisioning only when it answers ``None``. Keep it. It is the stronger
      question — it proves the credential can be **read back**, which is what the
      member's own consent calls then present — and it is a call that path already
      makes. The registry's per-role match is a floor, not a substitute: it says a
      credential exists, not that this service can resolve one.

    Order matters and matches :func:`revoke_user_identity` in reverse: credential,
    then membership, then the Keycloak mapping — the membership has a foreign key
    to the DID. A Keycloak sync that fails after its retries rolls both back.
    """
    base_url, headers = access.base_url, access.headers

    body: dict[str, Any] = {"subject_id": facts.subject_id, "role": facts.role}
    if facts.ttl_days is not None:
        body["ttl_days"] = facts.ttl_days
    if binding.linked_participant_did:
        body["linked_participant_did"] = binding.linked_participant_did
    if facts.allowed_actions:
        body["allowed_actions"] = list(facts.allowed_actions)
    if facts.verified_by:
        body["verified_by"] = facts.verified_by
    if facts.verification_method:
        body["verification_method"] = facts.verification_method

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base_url}/admin/credentials/data-subject",
            json=body,
            headers=headers,
        )
        if resp.status_code >= 400:
            raise ValueError(f"Credential issuance failed ({resp.status_code}): {resp.text}")

        evidence = resp.json()

    did: str | None = evidence.get("subjectDid")
    cred_id: str | None = evidence.get("credentialId")
    if not did or not cred_id:
        raise ValueError("Identity-registry response is missing subjectDid or credentialId")

    org_alias = binding.organization
    if org_alias:
        await _register_membership(base_url, headers, did, org_alias)

    if facts.keycloak_user_id and facts.keycloak_realm:
        await _sync_keycloak(
            base_url,
            headers,
            did=did,
            keycloak_user_id=facts.keycloak_user_id,
            keycloak_realm=facts.keycloak_realm,
            email=facts.email,
            username=facts.keycloak_username,
            credential_id=cred_id,
            organization_alias=org_alias,
        )

    return SubjectIdentity(
        did=did,
        credential_id=cred_id,
        issued_at=_parse_generated_at(evidence.get("generatedAt")),
    )


async def provision_user_identity(
    submission: Submission,
    *,
    keycloak_user_id: str | None = None,
    keycloak_realm: str | None = None,
    keycloak_username: str | None = None,
    provision_shares: bool = True,
) -> None:
    """The funnel's door into :func:`provision_subject`.

    Reads the facts off an approved submission, provisions, and writes the
    resulting identity back onto the row. **The authority here is a submission a
    REC manager approved**; the wizard's is the REC's offline preregistration,
    and both record the same assurance.

    ``keycloak_username`` is what provisioning read back from Keycloak. It is the
    same value that becomes ``Member.user_id`` in the REC registry, and passing
    it here is what lets the data plane join the two — see :func:`_sync_keycloak`.
    Optional, because a retry of this step alone has no provisioning result to
    read it from; the registry then falls back to the email, which is right for
    every user this service created and wrong only for one it adopted.
    """
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

    access = await registry_access()

    source = settings.dataspace_subject_source.strip().lower()
    if source in {"email_hash", "email"}:
        if not submission.email:
            raise ValueError("Cannot derive subject id: submission has no email")
        resolved = await resolve_subject(access, email=submission.email)
    elif source in {"submission_ref", "ref"}:
        # No resolve call, so no DID and no issuance guard. That is the honest
        # answer for this source: the subject id comes from the submission and the
        # registry has never been asked about this person.
        resolved = ResolvedSubject(subject_id=_submission_ref_subject_id(submission))
    else:
        raise ValueError("Unsupported DATASPACE_SUBJECT_SOURCE. Use email_hash or submission_ref.")

    identity = await provision_subject(
        access,
        SubjectFacts(
            subject_id=resolved.subject_id,
            role=settings.dataspace_user_role,
            email=submission.email,
            keycloak_user_id=keycloak_user_id,
            keycloak_realm=keycloak_realm,
            keycloak_username=keycloak_username,
            # The REC established this person's identity, by reviewing the
            # submission its manager approved. `organization_did` is the only
            # authority this service can name without inventing one.
            verified_by=binding.organization_did or None,
            verification_method=VERIFICATION_METHOD,
            allowed_actions=tuple(
                a.strip() for a in settings.dataspace_allowed_actions.split(",") if a.strip()
            ),
            ttl_days=settings.dataspace_vc_ttl_days,
        ),
        binding,
    )

    submission.dataspace_subject_id = resolved.subject_id
    submission.dataspace_did = identity.did
    submission.dataspace_vc_id = identity.credential_id
    submission.dataspace_vc_issued_at = identity.issued_at

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


async def provision_user_shares(submission: Submission, *, raise_on_error: bool = False) -> bool:
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


async def withdraw_user_shares(
    submission: Submission, *, reason: str = "", raise_on_error: bool = False
) -> bool:
    """Withdraw the standing consent this service provisioned.

    The mirror of :func:`provision_user_shares`, and it exists because the two
    have to be a pair. This service grants on the person's behalf at approval —
    same endpoint, same `connector.consent.provision` scope — so declining to
    un-grant on their behalf at revocation was an asymmetry, not a principle. It
    left a consent standing for somebody who is no longer a member, and (because
    revocation also deletes their credential) no way for them to withdraw it
    themselves.

    ``enabled: false`` is the same call with one boolean flipped: the connector
    moves the same row to ``revoked`` with a ``revocation_reason``, which is
    where *why* is recorded. It is the identical row and status the subject's own
    withdrawal produces — the two paths differ in which credential opens the
    door, not in what they write.

    Runs **before** the credential is deleted. Afterwards the connector would
    still accept the call, but the ordering keeps the sequence readable: undo the
    consent, then the identity that carried it.
    """
    if not settings.ds_connector_url:
        return False
    if not submission.dataspace_did:
        return False

    offer_ids = list(submission.data_sharing_consent_offer_ids or [])
    if not offer_ids:
        return False

    connector_url = settings.ds_connector_url.rstrip("/")
    headers = await _auth_headers()
    detail = reason or f"Membership revoked in {submission.rec_slug}"

    failures: list[str] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for offer_id in offer_ids:
            try:
                resp = await client.post(
                    f"{connector_url}/consent/admin/shares",
                    json={
                        "subject_id": submission.dataspace_did,
                        "offer_id": offer_id,
                        "enabled": False,
                        "message": detail,
                    },
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                failures.append(f"{offer_id}: {exc}")
                continue
            # 404 is success here: nothing to withdraw is the state we want.
            if resp.status_code >= 400 and resp.status_code != 404:
                failures.append(f"{offer_id}: {resp.status_code} {resp.text}")

    if failures:
        logger.error("Withdrawing shares for %s failed: %s", submission.ref, "; ".join(failures))
        if raise_on_error:
            raise ValueError("Share withdrawal failed: " + "; ".join(failures))
        return False

    submission.share_provisioned = False
    return True


async def _register_membership(
    base_url: str,
    headers: dict[str, str],
    did: str,
    org_alias: str,
) -> None:
    """Register the user DID as a member of the REC organization.

    Membership is what the ds consent endpoints check, so a user without it holds a
    valid credential but cannot manage data sharing.

    **A membership says where somebody belongs, not what they are there**, and
    that is why no role is sent. The registry used to accept one, store it in a
    column nothing read, and has since dropped the column; what a person is in a
    community is a ``communityRole`` claim on their data-subject credential,
    changed by reissuing it. Sending a role here recorded nothing while reading,
    to anybody who found it, as though it recorded something.

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
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f"{base_url}/admin/memberships", json=body, headers=headers)

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
        raise ValueError(f"Membership registration failed ({resp.status_code}): {resp.text}")


async def _delete_membership(
    base_url: str, headers: dict[str, str], did: str, org_alias: str
) -> None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.delete(f"{base_url}/admin/memberships/{did}/{org_alias}", headers=headers)
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
    username: str | None = None,
    credential_id: str,
    organization_alias: str = "",
) -> None:
    """Put the DID on the Keycloak user, and the username beside it.

    Fixes [#2](https://github.com/celine-eu/onboarding/issues/2).

    **The username is what the data plane joins on.** A dataspace decision names
    people by DID; the systems holding their data do not. The connector
    translates one to the other through this registry
    (`POST /users/identities` → `KeycloakMapping.username or .email`) and hands
    the answer to the celine `dataset-api`, which resolves it against
    `Member.user_id` — the value :func:`rec_registry.member_user_id` writes from
    the same provisioning result this argument comes from. Sending the username
    is what keeps both ends naming a person the same way.

    **Email is the registry's fallback, not a substitute.** Omitting the username
    leaves the connector resolving subjects by email, which is right only while
    username == email. That is this service's own convention for users it
    creates and explicitly not the platform's: `_find_user` also adopts a user
    whose username is something else, and for them the two ends would disagree —
    the row filter resolves nobody, the handler denies, and a person who
    consented silently gets no rows.
    """
    sync_body = {
        "did": did,
        "keycloak_realm": keycloak_realm,
        "keycloak_user_id": keycloak_user_id,
    }
    if email:
        sync_body["email"] = email
    if username:
        sync_body["username"] = username

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
                last_error = ValueError(f"KC sync failed ({resp.status_code}): {resp.text}")
        except httpx.HTTPError as exc:
            last_error = exc

        if attempt < _KC_SYNC_MAX_RETRIES:
            logger.warning("KC sync attempt %d/%d failed, retrying", attempt, _KC_SYNC_MAX_RETRIES)

    logger.error(
        "KC sync failed after %d attempts, revoking credential %s",
        _KC_SYNC_MAX_RETRIES,
        credential_id,
    )
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
        resp = await client.delete(f"{base_url}/admin/credentials/{credential_id}", headers=headers)
        # 404 is success for this purpose: the credential is gone either way, and
        # refusing to clear the local columns would make the state unrepairable.
        if resp.status_code >= 400 and resp.status_code != 404:
            raise ValueError(f"Credential revocation failed ({resp.status_code}): {resp.text}")

    submission.dataspace_vc_id = None
    submission.dataspace_did = None
    submission.dataspace_vc_issued_at = None
    submission.share_provisioned = False
    return f"revoked credential {credential_id}"
