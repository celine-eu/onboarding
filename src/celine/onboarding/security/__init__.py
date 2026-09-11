"""Authentication and authorization for the onboarding admin console.

The public wizard is anonymous and stays that way. Everything in this package
concerns `/api/admin/**` only.

The JWT claim readers this service authorises on are **not** re-exported here.
They are `celine.sdk.auth`'s — `realm_groups`, `organization_groups`,
`organization_aliases`, and the `Organization` that `JwtUser.get_organization`
returns — and importing them from the SDK is what keeps one reader in one place.
This package used to carry private copies.
"""

from celine.onboarding.security.policy import (
    ALL_CAPABILITIES,
    Capability,
    Decision,
    OnboardingAccessPolicy,
    get_policy,
)

__all__ = [
    "ALL_CAPABILITIES",
    "Capability",
    "Decision",
    "OnboardingAccessPolicy",
    "get_policy",
]
