"""An applicant who declares they are already a member of the community.

A template may offer the declaration (``existing_members.enabled``, REQ-0024).
An applicant who makes it is spared the steps the template names in
``skip_steps``. The POD is then **deferred**, not waived, and so is the supply
address when the template skips the coverage step (``eligibility``): the
operator completes them from the community's own member register, by revision,
and approval refuses until they have (REQ-0025). Where the coverage step is not
skipped, the declared member takes it like everyone else.

The declaration is the applicant's word and nothing checks it. That is why it
can only move who supplies the data, never make the data unneeded: somebody who
is not in the register is stuck at review, and the operator rejects them.
"""

from __future__ import annotations

from celine.onboarding.models.submission import Submission
from celine.onboarding.services import template_service

#: What a declared member may leave to the operator, as `Submission` fields.
POD_CODE = "pod_code"
SUPPLY_ADDRESS = "supply_address"


class DeclarationNotOfferedError(ValueError):
    """The applicant declared membership on a template that does not offer it."""


def declared(submission: Submission) -> bool:
    return bool(getattr(submission, "declared_existing_member", False))


def assert_offered(rec_slug: str) -> None:
    """Refuse a declaration the template does not offer, rather than ignore it."""
    if not template_service.existing_members_enabled(rec_slug):
        raise DeclarationNotOfferedError(
            "This community does not offer the existing-member declaration"
        )


def skipped_steps(submission: Submission) -> frozenset[str]:
    """The steps the wizard did not show this applicant."""
    if not declared(submission):
        return frozenset()
    block = template_service.existing_members(template_service.load_manifest(submission.rec_slug))
    return frozenset(block["skip_steps"])


def address_deferred(submission: Submission) -> bool:
    """Whether the supply address is the operator's to complete, not the applicant's.

    Only when the declared member was spared the coverage step: otherwise they
    checked their address there, under the same rules as everyone.
    """
    return "eligibility" in skipped_steps(submission)


def _operator_revised(submission: Submission, field: str) -> bool:
    return any(
        r.field == field and r.actor_type == "operator"
        for r in getattr(submission, "revisions", None) or []
    )


def pending(submission: Submission) -> list[str]:
    """What the operator must still complete before a declared member is approved.

    - the POD, when the applicant gave none;
    - the supply address, when the coverage step was skipped, until an operator
      has revised it. Nothing the applicant gave was checked against the areas,
      so only the register's address decides the area they are registered into.
    """
    if not declared(submission):
        return []
    missing = []
    if not submission.pod_code:
        missing.append(POD_CODE)
    if address_deferred(submission) and not _operator_revised(submission, SUPPLY_ADDRESS):
        missing.append(SUPPLY_ADDRESS)
    return missing


def assert_complete(submission: Submission) -> None:
    """Block approval of a declared member the operator has not completed."""
    missing = pending(submission)
    if missing:
        raise ValueError(
            "Cannot approve: the applicant declared they are already a member, and "
            f"{', '.join(missing)} must first be completed from the member register "
            "(POST .../revisions)"
        )
