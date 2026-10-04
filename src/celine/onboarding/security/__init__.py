"""Authentication and authorization for the onboarding admin console.

The public wizard is anonymous and stays that way. Everything in this package
concerns `/api/admin/**` only.

The JWT claim readers this service authorises on are **not** re-exported here.
They are `celine.sdk.auth`'s — `JwtUser.realm_roles` for the platform level, and
the `Organization` that `JwtUser.get_organization` returns for the organization
level — and importing them from the SDK is what keeps one reader in one place.
A realm group (the top-level `groups` claim) is read by nothing here.
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
