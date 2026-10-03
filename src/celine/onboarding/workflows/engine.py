from celine.onboarding.models.submission import Submission, SubmissionStatus
from celine.onboarding.services import existing_member, template_service

TRANSITIONS: dict[SubmissionStatus, set[SubmissionStatus]] = {
    SubmissionStatus.DRAFT: {SubmissionStatus.SUBMITTED},
    SubmissionStatus.SUBMITTED: {SubmissionStatus.UNDER_REVIEW, SubmissionStatus.REJECTED},
    SubmissionStatus.UNDER_REVIEW: {SubmissionStatus.APPROVED, SubmissionStatus.REJECTED},
    SubmissionStatus.APPROVED: set(),
    SubmissionStatus.REJECTED: {SubmissionStatus.SUBMITTED},
}


class InvalidTransitionError(ValueError):
    pass


def validate_transition(current: SubmissionStatus, target: SubmissionStatus) -> None:
    allowed = TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransitionError(
            f"Cannot transition from {current.value} to {target.value}. "
            f"Allowed: {', '.join(s.value for s in allowed) or 'none'}"
        )


def can_submit(submission: Submission) -> list[str]:
    """Check if a submission has all required fields for submission. Returns list of errors."""
    errors = []
    if not submission.first_name:
        errors.append("first_name is required")
    if not submission.last_name:
        errors.append("last_name is required")
    if not submission.fiscal_code:
        errors.append("fiscal_code is required")
    # A declared existing member leaves the POD to the operator, who completes it
    # from the register before approval (REQ-0025).
    if not submission.pod_code and not existing_member.declared(submission):
        errors.append("pod_code is required")
    if not submission.email and not submission.phone:
        errors.append("email or phone is required")
    # Only the documents the community declares: one it publishes nowhere was never
    # asked, so it cannot be missing (`template_service.consent_slots`).
    asked = template_service.consent_slots(submission.rec_slug)
    for slot, name in (("gdpr", "GDPR"), ("policy", "Policy"), ("statute", "Statute")):
        if slot in asked and not getattr(submission, f"{slot}_consent"):
            errors.append(f"{name} consent is required")

    manifest = template_service.load_manifest(submission.rec_slug)
    extra_fields = manifest.get("fields", {}).get("extra", [])
    extra_data = submission.extra_data or {}
    # A step the wizard never showed cannot have been answered (REQ-0024).
    skipped = existing_member.skipped_steps(submission)
    for field in extra_fields:
        if field.get("step") in skipped:
            continue
        if field.get("required") and not extra_data.get(field["key"]):
            errors.append(f"{field['key']} is required")

    return errors
