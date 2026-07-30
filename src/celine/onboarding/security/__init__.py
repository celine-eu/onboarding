"""Authentication and authorization for the onboarding admin console.

The public wizard is anonymous and stays that way. Everything in this package
concerns `/api/admin/**` only.
"""

from celine.onboarding.security.policy import (
    ALL_CAPABILITIES,
    Capability,
    Decision,
    OnboardingAccessPolicy,
    get_policy,
    organization_aliases,
    organization_groups,
    realm_groups,
)

__all__ = [
    "ALL_CAPABILITIES",
    "Capability",
    "Decision",
    "OnboardingAccessPolicy",
    "get_policy",
    "organization_aliases",
    "organization_groups",
    "realm_groups",
]
