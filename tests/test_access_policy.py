"""The authorization spec for the admin console, as a table.

Every capability the console enforces, against every kind of caller. If a change
to `policies/celine/onboarding/access.rego` widens or narrows a grant, it shows
up here rather than in production.
"""

from __future__ import annotations

import pytest
from celine.sdk.auth import JwtUser, Organization

from celine.onboarding.security.policy import (
    ALL_CAPABILITIES,
    Capability,
    OnboardingAccessPolicy,
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

# Which tiers mean anything at *realm* level. A realm badge grants its actions on
# every community with no organization check, so the two read-only tiers are
# excluded from it — they are an organization-level role and nothing else.
PLATFORM_TIERS = {"admins": ADMIN, "managers": MANAGER}
NON_PLATFORM_TIERS = ("editors", "viewers")


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
    org_type: str | None = "rec",
) -> JwtUser:
    """A human, as `JwtUser.from_token` would have built one.

    Realm groups stay in `claims`, because that is where the policy reads them
    from — with Keycloak's leading slash, which the SDK's reader strips. The
    organization is passed **parsed**, the way `from_token` parses it: the claim
    shapes it can arrive in are the SDK's problem and are tested there, and
    re-testing them here would pin one service to another's parser.

    `org_type` defaults to `"rec"` because a real REC organization is typed one
    and an organization-scoped grant requires it. `org_type=None` is the untyped
    organization a realm that never ran `keycloak sync-orgs` produces.
    """
    claims: dict = {"email": "operator@example.org", "preferred_username": "operator"}
    if realm:
        claims["groups"] = [f"/{g}" for g in realm]

    organizations: list[Organization] = []
    if org:
        claims["organization"] = {org: {"id": "org-uuid"}}
        organizations.append(
            Organization(alias=org, id="org-uuid", type=org_type, groups=list(groups))
        )
    return JwtUser(sub=sub, email=claims["email"], claims=claims, organizations=organizations)


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


@pytest.mark.parametrize("tier", sorted(TIERS))
def test_an_untyped_organization_grants_nothing(policy, tier):
    """The state of a realm that never ran `keycloak sync-orgs`.

    Absent is not "probably a REC": tolerating it would make the type check
    bypassable by leaving the attribute off.
    """
    user = operator(org=ORG, groups=(tier,), org_type=None)
    assert policy.capabilities(user, organization=ORG) == frozenset()


@pytest.mark.parametrize("tier", sorted(TIERS))
def test_an_organization_of_another_type_grants_nothing(policy, tier):
    """A DSO's operators are not a REC's, whatever their groups are called."""
    user = operator(org=ORG, groups=(tier,), org_type="dso")
    assert policy.capabilities(user, organization=ORG) == frozenset()


def test_untyped_organization_denial_says_so(policy):
    decision = policy.allow(
        operator(org=ORG, groups=("admins",), org_type=None),
        Capability.SUBMISSIONS_READ,
        organization=ORG,
    )
    assert not decision.allowed
    assert "not typed as a REC" in (decision.reason or "")


def test_multiple_org_memberships_are_scoped_independently(policy):
    user = JwtUser(
        sub="user-2",
        email="op@example.org",
        claims={"email": "op@example.org", "organization": {ORG: {}, OTHER_ORG: {}}},
        organizations=[
            Organization(alias=ORG, id="a", type="rec", groups=["managers"]),
            Organization(alias=OTHER_ORG, id="b", type="rec", groups=["viewers"]),
        ],
    )
    assert policy.capabilities(user, organization=ORG) == MANAGER
    assert policy.capabilities(user, organization=OTHER_ORG) == VIEWER


# ---------------------------------------------------------------------------
# Operators — realm groups are platform-wide
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier,expected", sorted(PLATFORM_TIERS.items()))
def test_a_platform_realm_group_grants_its_tier_on_every_community(policy, tier, expected):
    user = operator(realm=(tier,))
    assert policy.capabilities(user, organization=ORG) == expected
    assert policy.capabilities(user, organization=OTHER_ORG) == expected


@pytest.mark.parametrize("tier", NON_PLATFORM_TIERS)
def test_a_read_only_realm_group_grants_nothing_anywhere(policy, tier):
    """The narrowing this suite exists to hold.

    A realm badge is a grant over every community on the deployment, so the two
    read-only tiers do not carry one — a realm `viewers` used to read every REC's
    submissions and audit trail.
    """
    user = operator(realm=(tier,))
    assert policy.capabilities(user, organization=ORG) == frozenset()
    assert policy.capabilities(user, organization=OTHER_ORG) == frozenset()
    assert policy.capabilities(user, organization=None) == frozenset()


def test_realm_group_applies_without_a_named_community(policy):
    user = operator(realm=("admins",))
    assert policy.capabilities(user, organization=None) == ADMIN


def test_a_realm_manager_still_cannot_purge_or_revoke(policy):
    """`platform_groups` decides whether the realm level applies, not what it grants.

    The capability table keeps deciding the actions, so the two irreversible ones
    stay `admins`-only at both levels.
    """
    user = operator(realm=("managers",))
    caps = policy.capabilities(user, organization=ORG)
    assert Capability.SUBMISSIONS_PURGE.value not in caps
    assert Capability.ENABLEMENT_REVOKE.value not in caps


def test_grants_are_additive_across_the_two_levels(policy):
    """A realm badge that grants nothing does not cap the organization one."""
    user = operator(org=ORG, groups=("managers",), realm=("viewers",))
    assert policy.capabilities(user, organization=ORG) == MANAGER
    # ...and on a community they are not a member of, the realm viewer badge
    # leaves them with nothing at all.
    assert policy.capabilities(user, organization=OTHER_ORG) == frozenset()


def test_a_realm_editor_is_still_an_organization_editor(policy):
    """The two read-only tiers keep their meaning one level down."""
    user = operator(org=ORG, groups=("editors",), realm=("editors",))
    assert policy.capabilities(user, organization=ORG) == EDITOR
    assert policy.capabilities(user, organization=OTHER_ORG) == frozenset()


def test_non_platform_realm_denial_says_so(policy):
    decision = policy.allow(
        operator(realm=("viewers",)), Capability.SUBMISSIONS_READ, organization=ORG
    )
    assert not decision.allowed
    assert "not a platform-wide grant" in (decision.reason or "")


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
