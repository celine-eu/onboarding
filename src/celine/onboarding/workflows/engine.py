from celine.onboarding.models.submission import Submission, SubmissionStatus

TRANSITIONS: dict[SubmissionStatus, set[SubmissionStatus]] = {
    SubmissionStatus.DRAFT: {SubmissionStatus.SUBMITTED},
    SubmissionStatus.SUBMITTED: {SubmissionStatus.UNDER_REVIEW, SubmissionStatus.REJECTED},
    SubmissionStatus.UNDER_REVIEW: {SubmissionStatus.APPROVED, SubmissionStatus.REJECTED},
    SubmissionStatus.APPROVED: set(),
    SubmissionStatus.REJECTED: {SubmissionStatus.SUBMITTED},
}


class InvalidTransition(ValueError):
    pass


def validate_transition(current: SubmissionStatus, target: SubmissionStatus) -> None:
    allowed = TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransition(
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
    if not submission.pod_code:
        errors.append("pod_code is required")
    if not submission.email and not submission.phone:
        errors.append("email or phone is required")
    if not submission.gdpr_consent:
        errors.append("GDPR consent is required")
    if not submission.policy_consent:
        errors.append("Policy consent is required")
    if not submission.statute_consent:
        errors.append("Statute consent is required")
    return errors
