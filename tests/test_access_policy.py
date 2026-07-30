"""The authorization spec for the admin console, as a table.

Every capability the console enforces, against every kind of caller. If a change
to `policies/celine/onboarding/access.rego` widens or narrows a grant, it shows
up here rather than in production.
"""

from __future__ import annotations

import pytest
from celine.sdk.auth import JwtUser

from celine.onboarding.security.policy import (
    ALL_CAPABILITIES,
    Capability,
    OnboardingAccessPolicy,
    organization_aliases,
    organization_groups,
    realm_groups,
)

ORG = "my-rec"
OTHER_ORG = "other-rec"

# The role hierarchy, as capability sets. Written out rather than derived so that
# a change to the hierarchy has to be stated here too.
VIEWER = {"recs.read", "submissions.read", "audit.read"}
EDITOR = VIEWER | {"submissions.reveal", "submissions.write"}
MANAGER = EDITOR | {"submissions.review", "enablement.retry", "export"}
ADMIN = MANAGER | {"submissions.purge", "enablement.revoke"}

TIERS = {"viewers": VIEWER, "editors": EDITOR, "managers": MANAGER, "admins": ADMIN}


@pytest.fixture(scope="module")
def policy() -> OnboardingAccessPolicy:
    p = OnboardingAccessPolicy()
    assert p.available, f"policy bundle did not load: {p.load_error}"
    return p


# ---------------------------------------------------------------------------
# Callers
# ---------------------------------------------------------------------------


def operator(
    *,
    org: str | None = None,
    groups: tuple[str, ...] = (),
    realm: tuple[str, ...] = (),
    sub: str = "user-1",
) -> JwtUser:
    """A human. Keycloak emits group paths with a leading slash."""
    claims: dict = {"email": "operator@example.org", "preferred_username": "operator"}
    if realm:
        claims["groups"] = [f"/{g}" for g in realm]
    if org:
        claims["organization"] = {org: {"id": "org-uuid", "groups": [f"/{g}" for g in groups]}}
    return JwtUser(sub=sub, email=claims["email"], claims=claims)


def service(*scopes: str) -> JwtUser:
    """A client_credentials token: no organization, no groups, only scopes."""
    return JwtUser(
        sub="service-account-uuid",
        claims={
            "preferred_username": "service-account-svc-onboarding-cli",
            "client_id": "svc-onboarding-cli",
            "scope": " ".join(scopes),
        },
    )


# ---------------------------------------------------------------------------
# Operators — organization-scoped groups
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier,expected", sorted(TIERS.items()))
def test_org_group_grants_exactly_its_tier(policy, tier, expected):
    user = operator(org=ORG, groups=(tier,))
    assert policy.capabilities(user, organization=ORG) == expected


@pytest.mark.parametrize("tier", sorted(TIERS))
def test_org_group_grants_nothing_on_another_community(policy, tier):
    """The tenancy boundary. A managers badge in one REC is not a badge in another."""
    user = operator(org=ORG, groups=(tier,))
    assert policy.capabilities(user, organization=OTHER_ORG) == frozenset()


def test_org_membership_without_a_group_grants_nothing(policy):
    user = operator(org=ORG, groups=())
    assert policy.capabilities(user, organization=ORG) == frozenset()


def test_no_organization_and_no_group_grants_nothing(policy):
    assert policy.capabilities(operator(), organization=ORG) == frozenset()


def test_org_admin_is_denied_when_no_community_is_named(policy):
    """`organization=None` cannot match an org group, so only realm grants apply."""
    user = operator(org=ORG, groups=("admins",))
    assert policy.capabilities(user, organization=None) == frozenset()


def test_multiple_org_memberships_are_scoped_independently(policy):
    user = JwtUser(
        sub="user-2",
        email="op@example.org",
        claims={
            "email": "op@example.org",
            "organization": {
                ORG: {"id": "a", "groups": ["/managers"]},
                OTHER_ORG: {"id": "b", "groups": ["/viewers"]},
            },
        },
    )
    assert policy.capabilities(user, organization=ORG) == MANAGER
    assert policy.capabilities(user, organization=OTHER_ORG) == VIEWER


# ---------------------------------------------------------------------------
# Operators — realm groups are platform-wide
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier,expected", sorted(TIERS.items()))
def test_realm_group_grants_its_tier_on_every_community(policy, tier, expected):
    user = operator(realm=(tier,))
    assert policy.capabilities(user, organization=ORG) == expected
    assert policy.capabilities(user, organization=OTHER_ORG) == expected


def test_realm_group_applies_without_a_named_community(policy):
    user = operator(realm=("admins",))
    assert policy.capabilities(user, organization=None) == ADMIN


def test_highest_tier_wins_when_realm_and_org_disagree(policy):
    """Grants are additive: the realm viewer badge does not cap the org manager one."""
    user = operator(org=ORG, groups=("managers",), realm=("viewers",))
    assert policy.capabilities(user, organization=ORG) == MANAGER
    # ...and on a community they are not a member of, only the realm badge counts.
    assert policy.capabilities(user, organization=OTHER_ORG) == VIEWER


# ---------------------------------------------------------------------------
# Services — scopes, not groups
# ---------------------------------------------------------------------------


def test_service_admin_scope_grants_everything(policy):
    caps = policy.capabilities(service("onboarding.admin"), organization=ORG)
    assert caps == {c.value for c in ALL_CAPABILITIES}


def test_service_with_no_scope_gets_nothing(policy):
    assert policy.capabilities(service(), organization=ORG) == frozenset()


@pytest.mark.parametrize(
    "scope,capability",
    [
        ("onboarding.recs.read", Capability.RECS_READ),
        ("onboarding.submissions.read", Capability.SUBMISSIONS_READ),
        ("onboarding.submissions.reveal", Capability.SUBMISSIONS_REVEAL),
        ("onboarding.submissions.write", Capability.SUBMISSIONS_WRITE),
        ("onboarding.submissions.review", Capability.SUBMISSIONS_REVIEW),
        ("onboarding.submissions.purge", Capability.SUBMISSIONS_PURGE),
        ("onboarding.enablement.retry", Capability.ENABLEMENT_RETRY),
        ("onboarding.enablement.revoke", Capability.ENABLEMENT_REVOKE),
        ("onboarding.audit.read", Capability.AUDIT_READ),
        ("onboarding.export", Capability.EXPORT),
    ],
)
def test_narrow_service_scope_grants_exactly_one_capability(policy, scope, capability):
    caps = policy.capabilities(service(scope), organization=ORG)
    assert caps == {capability.value}


def test_review_scope_does_not_grant_purge_or_revoke(policy):
    """Rejecting is recoverable; erasing and revoking are not."""
    caps = policy.capabilities(service("onboarding.submissions.review"), organization=ORG)
    assert Capability.SUBMISSIONS_PURGE.value not in caps
    assert Capability.ENABLEMENT_REVOKE.value not in caps


def test_service_is_not_organization_scoped(policy):
    """Documented consequence: a service account has no organization to check.

    It is authorised by scope alone and can therefore act on any community. The
    CLI is a break-glass and e2e driver, so this is intended — but it is the
    reason a narrow scope matters more for services than for operators.
    """
    svc = service("onboarding.submissions.review")
    for org in (ORG, OTHER_ORG, "a-community-that-does-not-exist"):
        assert policy.allow(svc, Capability.SUBMISSIONS_REVIEW, organization=org).allowed


def test_group_named_scope_does_not_authorise_a_service(policy):
    """A service carrying an org-shaped group claim is still scope-checked.

    Guards the subject-typing rule: presence of groups makes a caller a *user*,
    and a user with no matching organization gets nothing — it must not fall
    through to the service branch and be allowed by its scopes.
    """
    hybrid = JwtUser(
        sub="odd-token",
        claims={
            "preferred_username": "service-account-svc-onboarding-cli",
            "groups": ["/admins"],
            "scope": "onboarding.admin",
        },
    )
    # Typed as a user because groups are present; the realm admins group is what
    # grants it — not the scope.
    assert policy.capabilities(hybrid, organization=ORG) == ADMIN


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_unknown_capability_is_denied(policy):
    decision = policy.allow(operator(realm=("admins",)), "submissions.teleport", organization=ORG)
    assert not decision.allowed
    assert "unknown action" in (decision.reason or "")


def test_cross_organization_denial_says_so(policy):
    decision = policy.allow(
        operator(org=ORG, groups=("admins",)),
        Capability.SUBMISSIONS_READ,
        organization=OTHER_ORG,
    )
    assert not decision.allowed
    assert "different organization" in (decision.reason or "")


def test_insufficient_tier_denial_says_so(policy):
    decision = policy.allow(
        operator(org=ORG, groups=("viewers",)),
        Capability.SUBMISSIONS_REVIEW,
        organization=ORG,
    )
    assert not decision.allowed
    assert "no group grants this action" in (decision.reason or "")


def test_service_missing_scope_denial_says_so(policy):
    decision = policy.allow(
        service("onboarding.submissions.read"),
        Capability.SUBMISSIONS_PURGE,
        organization=ORG,
    )
    assert not decision.allowed
    assert "missing a scope" in (decision.reason or "")


def test_grant_reasons_name_the_level(policy):
    org_decision = policy.allow(
        operator(org=ORG, groups=("admins",)), Capability.SUBMISSIONS_PURGE, organization=ORG
    )
    assert org_decision.allowed
    assert org_decision.reason == "granted by organization group"

    realm_decision = policy.allow(
        operator(realm=("admins",)), Capability.SUBMISSIONS_PURGE, organization=ORG
    )
    assert realm_decision.allowed
    assert realm_decision.reason == "granted by realm group"


# ---------------------------------------------------------------------------
# Unloadable bundle
# ---------------------------------------------------------------------------


def test_missing_policy_bundle_denies_by_default(tmp_path):
    broken = OnboardingAccessPolicy(policies_dir=tmp_path / "nope")
    assert not broken.available
    decision = broken.allow(
        operator(realm=("admins",)), Capability.SUBMISSIONS_READ, organization=ORG
    )
    assert not decision.allowed
    assert decision.reason == "authorization unavailable"


def test_missing_policy_bundle_is_permissive_only_when_asked(tmp_path, monkeypatch):
    from celine.onboarding.config.settings import settings

    monkeypatch.setattr(settings, "allow_permissive_policy", True)
    broken = OnboardingAccessPolicy(policies_dir=tmp_path / "nope")
    decision = broken.allow(operator(), Capability.SUBMISSIONS_PURGE, organization=ORG)
    assert decision.allowed
    assert decision.reason == "policy-engine-unavailable-permissive"


# ---------------------------------------------------------------------------
# Claim readers
# ---------------------------------------------------------------------------


class TestClaimReaders:
    def test_realm_groups_strips_slashes_and_deduplicates(self):
        assert realm_groups({"groups": ["/managers", "managers", "/viewers"]}) == [
            "managers",
            "viewers",
        ]

    def test_realm_groups_ignores_organization_groups(self):
        """The whole reason these are read apart from `extract_groups`."""
        claims = {"organization": {ORG: {"groups": ["/admins"]}}}
        assert realm_groups(claims) == []

    def test_realm_groups_tolerates_junk(self):
        assert realm_groups({}) == []
        assert realm_groups({"groups": None}) == []
        assert realm_groups({"groups": "not-a-list"}) == []
        assert realm_groups({"groups": [1, None, "/ok"]}) == ["ok"]

    def test_organization_groups_are_per_alias(self):
        claims = {
            "organization": {
                ORG: {"groups": ["/managers"]},
                OTHER_ORG: {"groups": ["/viewers"]},
            }
        }
        assert organization_groups(claims, ORG) == ["managers"]
        assert organization_groups(claims, OTHER_ORG) == ["viewers"]
        assert organization_groups(claims, "unknown") == []

    def test_organization_groups_tolerate_the_no_groups_shape(self):
        """KC omits `groups` unless the org membership mapper includes roles."""
        claims = {"organization": {ORG: {"id": "x", "type": ["rec"]}}}
        assert organization_groups(claims, ORG) == []

    def test_organization_aliases(self):
        claims = {"organization": {OTHER_ORG: {}, ORG: {}}}
        assert organization_aliases(claims) == sorted([ORG, OTHER_ORG])
        assert organization_aliases({}) == []
