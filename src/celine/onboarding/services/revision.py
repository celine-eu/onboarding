"""Correcting what a participant declared, as a tracked revision.

From `submitted` on, a POD, first name, last name, email, fiscal code or supply
address changes only here: the admin `PATCH` refuses them and the wizard `PATCH`
refuses everything. Each
correction is one append-only `SubmissionRevision` row, written in the same
transaction as the `Submission` column it corrects and the audit row that says it
happened, so none of the three exists without the others.

Who may correct what, and on which evidence, is an `EvidencePolicy`:

- `OPERATOR` — the REC's operator, checked by hand (`offline`) or against a
  document on this submission (`uploaded-document`), with a required note. Any of
  the six fields. The fiscal code and the supply address are also how the
  operator completes a declared existing member from the community's register
  (REQ-0025, REQ-0026).
- `MEMBER` — the member's own correction from their authenticated session, for
  the later self-service route in the web app. Names and email only: the POD
  stays the operator's. No route uses it yet.

Before approval nothing else holds a copy, so the revision is the whole story:
approval reads the corrected column. After approval the value also lives in
Keycloak, the REC registry, the identity registry or the connector's consent
keys; `RevisionOutcome.propagation` names the steps that carry it there, and
`services/propagation.py` runs them.
"""

from __future__ import annotations

import enum
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.models.revision import SubmissionRevision
from celine.onboarding.models.submission import Submission, SubmissionStatus
from celine.onboarding.models.verification import VerificationMethod
from celine.onboarding.services import audit_service, document_service, enablement
from celine.onboarding.services.audit_service import Actor
from celine.onboarding.validators.fiscal_code import validate_fiscal_code
from celine.onboarding.validators.pod_code import validate_pod_code

logger = logging.getLogger(__name__)


class RevisableField(enum.StrEnum):
    """The `Submission` columns a revision corrects."""

    FIRST_NAME = "first_name"
    LAST_NAME = "last_name"
    EMAIL = "email"
    POD_CODE = "pod_code"
    #: Never leaves this service (PDF, CSV export, console), so it propagates
    #: nowhere and may be corrected after approval too.
    FISCAL_CODE = "fiscal_code"
    #: The text of the supply address the boundary is resolved from. Before
    #: approval only: afterwards a new address would mean moving the member to
    #: another registry area, which no propagation step does.
    SUPPLY_ADDRESS = "supply_address"


#: What the admin `PATCH` refuses from `submitted` on.
REVISED_FIELDS = frozenset(f.value for f in RevisableField)

#: A `rejected` submission is reopened (it becomes `submitted`) before it is
#: corrected; a draft is still the person's to edit in the wizard.
REVISABLE = frozenset(
    {SubmissionStatus.SUBMITTED, SubmissionStatus.UNDER_REVIEW, SubmissionStatus.APPROVED}
)

#: Fields whose revision stops at approval (see `RevisableField.SUPPLY_ADDRESS`).
BEFORE_APPROVAL_ONLY = frozenset({RevisableField.SUPPLY_ADDRESS})

#: Same limits as `SubmissionUpdate`: a correction is held to what a declaration is.
MAX_LENGTH = {
    RevisableField.FIRST_NAME: 100,
    RevisableField.LAST_NAME: 100,
    RevisableField.EMAIL: 255,
    RevisableField.POD_CODE: 20,
    RevisableField.FISCAL_CODE: 16,
    # `SupplyAddress.text`.
    RevisableField.SUPPLY_ADDRESS: 300,
}

NOTE_MAX_LENGTH = 1000


class RevisionMethod(enum.StrEnum):
    """How the new value was checked.

    The first two are `VerificationMethod`'s, so an operator's revision and their
    verification read the same way.
    """

    OFFLINE = VerificationMethod.OFFLINE.value
    UPLOADED_DOCUMENT = VerificationMethod.UPLOADED_DOCUMENT.value
    #: The member corrected it themselves, signed in.
    MEMBER_SESSION = "member-session"


class ActorRole(enum.StrEnum):
    """The role a revision was made in; stored as the row's `actor_type`."""

    OPERATOR = "operator"
    MEMBER = "member"


@dataclass(frozen=True)
class EvidencePolicy:
    role: ActorRole
    fields: frozenset[RevisableField]
    methods: frozenset[RevisionMethod]
    note_required: bool


OPERATOR = EvidencePolicy(
    role=ActorRole.OPERATOR,
    fields=frozenset(RevisableField),
    methods=frozenset({RevisionMethod.OFFLINE, RevisionMethod.UPLOADED_DOCUMENT}),
    note_required=True,
)

MEMBER = EvidencePolicy(
    role=ActorRole.MEMBER,
    fields=frozenset({RevisableField.FIRST_NAME, RevisableField.LAST_NAME, RevisableField.EMAIL}),
    methods=frozenset({RevisionMethod.MEMBER_SESSION}),
    note_required=False,
)


class PropagationStep(enum.StrEnum):
    """Where a corrected value goes after approval, one step per target.

    Run by `services/propagation.py`.
    """

    #: Keycloak's first/last name and email, through the provisioning service.
    ACCOUNT_PROFILE = "account_profile"
    #: The invitation to set a password, again, to the new address — only when
    #: the member never set one (the provisioning service checks, `has_password`).
    INVITATION = "invitation"
    #: The REC registry member's `name`.
    REGISTRY_NAME = "registry_name"
    #: The identity registry's Keycloak mapping, same DID, new email.
    IDENTITY_MAPPING = "identity_mapping"
    #: The registry delivery point, replacing the old one.
    REGISTRY_DELIVERY_POINT = "registry_delivery_point"
    #: The member's standing grants, re-sent with the new keys.
    CONSENT_KEYS = "consent_keys"


PROPAGATION: dict[RevisableField, tuple[PropagationStep, ...]] = {
    RevisableField.FIRST_NAME: (PropagationStep.ACCOUNT_PROFILE, PropagationStep.REGISTRY_NAME),
    RevisableField.LAST_NAME: (PropagationStep.ACCOUNT_PROFILE, PropagationStep.REGISTRY_NAME),
    RevisableField.EMAIL: (
        PropagationStep.ACCOUNT_PROFILE,
        PropagationStep.INVITATION,
        PropagationStep.IDENTITY_MAPPING,
    ),
    RevisableField.POD_CODE: (
        PropagationStep.REGISTRY_DELIVERY_POINT,
        PropagationStep.CONSENT_KEYS,
    ),
    RevisableField.FISCAL_CODE: (),
    RevisableField.SUPPLY_ADDRESS: (),
}

#: Why every step of a revision on a revoked member is skipped.
SKIP_ENABLEMENT_REVOKED = "enablement revoked"


@dataclass(frozen=True)
class RevisionOutcome:
    """What a recorded revision leaves for propagation to do.

    `propagation` is empty before approval: nothing else holds a copy yet. After
    approval it lists the steps for the field, in order; with `skip_reason` set,
    every one of them is to be recorded `skipped` with that reason.
    """

    revision: SubmissionRevision
    submission: Submission
    field: RevisableField
    previous_value: str | None
    new_value: str
    propagation: tuple[PropagationStep, ...]
    skip_reason: str | None = None


class RevisionError(ValueError):
    """The revision cannot be recorded as asked."""


class RevisionStatusError(RevisionError):
    """The submission is in a status that takes no revision."""


def normalise(field: RevisableField, value: str) -> str:
    """The value as the column will hold it, or `RevisionError`.

    A correction replaces a value, it does not erase one: the POD and the names are
    required at submit, and an empty email would leave the account with none.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        raise RevisionError(f"A revision of {field.value} needs a value")
    if len(cleaned) > MAX_LENGTH[field]:
        raise RevisionError(f"{field.value} is longer than {MAX_LENGTH[field]} characters")
    if field is RevisableField.POD_CODE:
        if not validate_pod_code(cleaned):
            raise RevisionError("Invalid POD code")
        return cleaned.upper()
    if field is RevisableField.FISCAL_CODE:
        if not validate_fiscal_code(cleaned):
            raise RevisionError("Invalid fiscal code")
        return cleaned.upper()
    return cleaned


def current_value(submission: Submission, field: RevisableField) -> str | None:
    """The value in force, as a revision records it: the supply address as its text."""
    if field is RevisableField.SUPPLY_ADDRESS:
        address = submission.supply_address
        return address.get("text") if isinstance(address, dict) else None
    return getattr(submission, field.value)


def _apply(submission: Submission, field: RevisableField, value: str) -> None:
    if field is RevisableField.SUPPLY_ADDRESS:
        submission.supply_address = {"text": value}
    else:
        setattr(submission, field.value, value)


def history(submission: Submission, field: RevisableField) -> list[SubmissionRevision]:
    """Every revision of one field, oldest first. The last is in force."""
    return [r for r in submission.revisions if r.field == field.value]


def declared_value(submission: Submission, field: RevisableField) -> str | None:
    """What the person declared: the first revision's previous value, or the column."""
    revisions = history(submission, field)
    if revisions:
        return revisions[0].previous_value
    return current_value(submission, field)


async def _propagation_for(
    db: AsyncSession, submission: Submission, field: RevisableField
) -> tuple[tuple[PropagationStep, ...], str | None]:
    if submission.status != SubmissionStatus.APPROVED:
        return (), None
    rows = await enablement.load_steps(db, submission.id)
    skip = SKIP_ENABLEMENT_REVOKED if enablement.revoked(rows) else None
    return PROPAGATION[field], skip


async def record(
    db: AsyncSession,
    submission: Submission,
    *,
    field: RevisableField,
    value: str,
    method: RevisionMethod,
    actor: Actor,
    policy: EvidencePolicy = OPERATOR,
    document_id: uuid.UUID | None = None,
    note: str | None = None,
    ip: str | None = None,
    rec_slug: str | None = None,
) -> RevisionOutcome:
    """Record one correction and update the column, in one transaction."""
    if submission.status not in REVISABLE:
        raise RevisionStatusError(
            f"A revision can be recorded only while a submission is submitted, under "
            f"review or approved, not {submission.status.value}"
        )
    if field in BEFORE_APPROVAL_ONLY and submission.status == SubmissionStatus.APPROVED:
        raise RevisionStatusError(
            f"{field.value} can be revised only before approval: the member is "
            "already registered in the area it resolved to"
        )
    if field not in policy.fields:
        raise RevisionError(f"A {policy.role.value} cannot revise {field.value}")
    if method not in policy.methods:
        raise RevisionError(f"A {policy.role.value} revision cannot use method {method.value}")

    cleaned_note = (note or "").strip() or None
    if policy.note_required and not cleaned_note:
        raise RevisionError("A revision needs a note saying how the new value was checked")
    if cleaned_note and len(cleaned_note) > NOTE_MAX_LENGTH:
        raise RevisionError(f"The note is longer than {NOTE_MAX_LENGTH} characters")

    if method is RevisionMethod.UPLOADED_DOCUMENT:
        if document_id is None:
            raise RevisionError("An uploaded-document revision must name the document")
        document = await document_service.get_document(db, document_id)
        # Same answer for "no such document" and "another submission's document",
        # as for verifications.
        if not document or document.submission_id != submission.id:
            raise RevisionError("The document is not stored on this submission")
    elif document_id is not None:
        raise RevisionError(f"A {method.value} revision names no document")

    new_value = normalise(field, value)
    previous_value = current_value(submission, field)
    if previous_value == new_value:
        raise RevisionError(f"{field.value} already holds this value")

    propagation, skip_reason = await _propagation_for(db, submission, field)

    superseded = history(submission, field)
    row = SubmissionRevision(
        # Set here rather than left to the column defaults, which apply only at
        # flush: the caller reads both straight back, and `created_at` orders the
        # history.
        id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        field=field.value,
        previous_value=previous_value,
        new_value=new_value,
        method=method.value,
        document_id=document_id,
        note=cleaned_note,
        actor_type=policy.role.value,
        actor_sub=actor.sub,
        actor_email=actor.email,
        actor_client_id=actor.client_id,
    )
    submission.revisions.append(row)
    if field is RevisableField.SUPPLY_ADDRESS:
        # The recorded boundary follows the address, as on a wizard save.
        from celine.onboarding.services import supply_boundary

        address_before = supply_boundary.supply_address(submission)
        _apply(submission, field, new_value)
        await supply_boundary.refresh_on_save(submission, address_before=address_before)
    else:
        _apply(submission, field, new_value)

    # Field, method and ids only. Never the values, never the note: the trail is
    # readable by every tier that can read the queue, and the POD is masked there.
    detail = f"field={field.value} revision={row.id} method={method.value}"
    if document_id is not None:
        detail = f"{detail} document={document_id}"
    if superseded:
        detail = f"{detail} supersedes={superseded[-1].id}"
    audit_service.record(
        db,
        action="revise",
        entity_type="submission",
        entity_id=str(submission.id),
        actor=actor,
        rec_slug=rec_slug or submission.rec_slug,
        ip=ip,
        detail=detail,
    )
    await db.commit()
    # The UPDATE expired `updated_at` (server `onupdate`); without the refresh a
    # caller serialising the submission would lazy-load it outside the greenlet.
    await db.refresh(submission)
    await db.refresh(row)
    logger.info(
        "Revision %s of %s recorded on %s by a %s",
        row.id,
        field.value,
        submission.ref,
        policy.role.value,
    )
    return RevisionOutcome(
        revision=row,
        submission=submission,
        field=field,
        previous_value=previous_value,
        new_value=new_value,
        propagation=propagation,
        skip_reason=skip_reason,
    )
