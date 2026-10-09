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
# The drift check (`recs.drift`, REQ-0015, D55): the REC's own managers and
# admins, and the platform admin; no scope.
DRIFT = {"recs.drift"}
# `submissions.revise` (a correction by revision) is granted where review is.
MANAGER = (
    EDITOR | {"submissions.review", "submissions.revise", "enablement.retry", "export"} | DRIFT
)
ADMIN = MANAGER | {"submissions.purge", "enablement.revoke"}

# Reachable only by a service acting for an operator, so no caller holds it alone
# and no capability set below contains it.
DELEGATED = {"members.invite", "members.release"}

# Granted at the platform level only: the realm role `platform-admin`, or a
# service scope (`onboarding.recs.write`, covered by `onboarding.admin`); never an
# organization group (the registry sync, REQ-0009).
PLATFORM_ONLY = {"recs.write"}
# Everything a person can hold, on every community (REQ-0030).
PLATFORM_ADMIN = ADMIN | PLATFORM_ONLY
PLATFORM_ADMIN_ROLE = "platform-admin"

TIERS = {"viewers": VIEWER, "editors": EDITOR, "managers": MANAGER, "admins": ADMIN}
READ_ONLY_TIERS = ("editors", "viewers")


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
    roles: tuple[str, ...] = (),
    realm_groups: tuple[str, ...] = (),
    sub: str = "user-1",
    org_type: str | None = "rec",
) -> JwtUser:
    """A human, as `JwtUser.from_token` would have built one.

    Realm roles stay in `claims`, under `realm_access.roles`, because that is
    where the SDK's reader takes them from. `realm_groups` writes a legacy
    top-level `groups` claim with Keycloak's leading slash: the realm groups a
    token may still carry, which must grant nothing. The organization is passed
    **parsed**, the way `from_token` parses it: the claim
    shapes it can arrive in are the SDK's problem and are tested there, and
    re-testing them here would pin one service to another's parser.

    `org_type` defaults to `"rec"` because a real REC organization is typed one
    and an organization-scoped grant requires it. `org_type=None` is the untyped
    organization a realm that never ran `keycloak sync-orgs` produces.
    """
    claims: dict = {"email": "operator@example.org", "preferred_username": "operator"}
    if roles:
        claims["realm_access"] = {"roles": list(roles)}
    if realm_groups:
        claims["groups"] = [f"/{g}" for g in realm_groups]

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
    """`organization=None` cannot match an org group, so only the platform role applies."""
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
# Operators — the platform-admin role is platform-wide; a realm group is nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("community", [ORG, OTHER_ORG, None])
def test_the_platform_admin_role_grants_everything_on_every_community(policy, community):
    """
    @verifies REQ-0030
    """
    user = operator(roles=(PLATFORM_ADMIN_ROLE,))
    assert policy.capabilities(user, organization=community) == PLATFORM_ADMIN


@pytest.mark.parametrize("tier", sorted(TIERS))
def test_a_realm_group_grants_nothing_anywhere(policy, tier):
    """A realm group still present in a token is not a grant, `admins` included.

    @verifies REQ-0030
    """
    user = operator(realm_groups=(tier,))
    for community in (ORG, OTHER_ORG, None):
        assert policy.capabilities(user, organization=community) == frozenset()


def test_a_bare_realm_group_name_grants_nothing(policy):
    """The client-level mapper wrote `admins` without the slash; neither form counts.

    @verifies REQ-0030
    """
    user = operator()
    user.claims["groups"] = ["/admins", "admins", "platform-admin", "/platform-admin"]
    assert policy.capabilities(user, organization=ORG) == frozenset()
    assert policy.capabilities(user, organization=None) == frozenset()


@pytest.mark.parametrize(
    "role", ["admin", "manager", "editor", "viewer", "admins", "default-roles-celine"]
)
def test_no_other_realm_role_grants_anything(policy, role):
    """The retired realm roles named like the tiers are not platform grants either.

    @verifies REQ-0030
    """
    user = operator(roles=(role, "offline_access", "uma_authorization"))
    assert policy.capabilities(user, organization=ORG) == frozenset()
    assert policy.capabilities(user, organization=None) == frozenset()


def test_the_role_is_read_from_realm_access_only(policy):
    """Not a top-level `roles` claim, not `groups`, not a client's roles.

    @verifies REQ-0030
    """
    user = operator()
    user.claims.update(
        {
            "roles": [PLATFORM_ADMIN_ROLE],
            "groups": [PLATFORM_ADMIN_ROLE],
            "resource_access": {"some-client": {"roles": [PLATFORM_ADMIN_ROLE]}},
        }
    )
    assert policy.capabilities(user, organization=ORG) == frozenset()


def test_an_organization_admin_is_not_a_platform_admin(policy):
    """An organization's own `admins` administer that REC and nothing else.

    @verifies REQ-0030
    """
    user = operator(org=ORG, groups=("admins",))
    assert policy.capabilities(user, organization=ORG) == ADMIN
    assert policy.capabilities(user, organization=OTHER_ORG) == frozenset()
    assert policy.capabilities(user, organization=None) == frozenset()
    assert not policy.allow(user, Capability.RECS_WRITE, organization=ORG).allowed


def test_a_legacy_realm_admins_group_does_not_widen_an_organization_grant(policy):
    """The pre-role platform admin: a realm `/admins` beside an organization group.

    @verifies REQ-0030
    """
    user = operator(org=ORG, groups=("managers",), realm_groups=("admins",))
    assert policy.capabilities(user, organization=ORG) == MANAGER
    assert policy.capabilities(user, organization=OTHER_ORG) == frozenset()


def test_the_role_and_an_organization_group_are_additive(policy):
    """A platform admin who is also a viewer of one REC is a platform admin there."""
    user = operator(org=ORG, groups=("viewers",), roles=(PLATFORM_ADMIN_ROLE,))
    assert policy.capabilities(user, organization=ORG) == PLATFORM_ADMIN
    assert policy.capabilities(user, organization=OTHER_ORG) == PLATFORM_ADMIN


def test_a_service_holding_the_role_is_still_judged_by_its_scopes(policy):
    """The role is read for people; a service is authorised by scope alone.

    @verifies REQ-0030
    """
    svc = service()
    svc.claims["realm_access"] = {"roles": [PLATFORM_ADMIN_ROLE]}
    assert policy.capabilities(svc, organization=ORG) == frozenset()


def test_the_policy_input_keeps_the_two_levels_apart(policy):
    """Roles in `subject.roles`; `subject.groups` empty; one organization's groups.

    @verifies REQ-0030
    """
    user = JwtUser(
        sub="user-3",
        email="op@example.org",
        claims={
            "email": "op@example.org",
            "groups": ["/admins"],
            "realm_access": {"roles": [PLATFORM_ADMIN_ROLE, "offline_access"]},
        },
        organizations=[
            Organization(alias=ORG, id="a", type="rec", groups=["viewers"]),
            Organization(alias=OTHER_ORG, id="b", type="rec", groups=["admins"]),
        ],
    )
    subject = policy._policy_input(user, "submissions.read", ORG)["subject"]
    # The SDK's serialiser (celine-sdk 2.0.0): all six keys, roles beside groups.
    assert list(subject) == ["id", "type", "roles", "groups", "scopes", "claims"]
    assert subject["type"] == "user"
    assert subject["groups"] == []
    assert subject["roles"] == [PLATFORM_ADMIN_ROLE, "offline_access"]
    assert subject["claims"] == {"organization": ORG, "org_groups": ["viewers"], "org_type": "rec"}


# ---------------------------------------------------------------------------
# Services — scopes, not groups
# ---------------------------------------------------------------------------


SERVICE_ADMIN = {c.value for c in ALL_CAPABILITIES} - DELEGATED - DRIFT


def test_service_admin_scope_grants_everything_but_delegated_and_people_only_actions(policy):
    caps = policy.capabilities(service("onboarding.admin"), organization=ORG)
    assert caps == SERVICE_ADMIN


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
        ("onboarding.submissions.revise", Capability.SUBMISSIONS_REVISE),
        ("onboarding.submissions.purge", Capability.SUBMISSIONS_PURGE),
        ("onboarding.enablement.retry", Capability.ENABLEMENT_RETRY),
        ("onboarding.enablement.revoke", Capability.ENABLEMENT_REVOKE),
        ("onboarding.audit.read", Capability.AUDIT_READ),
        ("onboarding.export", Capability.EXPORT),
        ("onboarding.recs.write", Capability.RECS_WRITE),
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


@pytest.mark.parametrize("scope,expected", [("onboarding.admin", SERVICE_ADMIN), ("", set())])
def test_a_realm_group_on_a_service_token_grants_nothing(policy, scope, expected):
    """A service token carrying a realm `admins` group is a service, judged by scope.

    Person or service is the SDK's `is_service_account`, never "has a group": a
    `service-account-` username is a client-credentials token whatever else it
    carries, and the realm group adds nothing to what its scopes grant.

    @verifies REQ-0030
    """
    hybrid = JwtUser(
        sub="odd-token",
        claims={
            "preferred_username": "service-account-svc-onboarding-cli",
            "groups": ["/admins"],
            "scope": scope,
        },
    )
    assert policy.capabilities(hybrid, organization=ORG) == expected


# ---------------------------------------------------------------------------
# recs.write — the registry sync, the platform level only: the platform-admin
# role, or the platform operator's client by scope (ADR-0017)
# ---------------------------------------------------------------------------

SYNC = Capability.RECS_WRITE


@pytest.mark.parametrize("community", [ORG, OTHER_ORG, None])
def test_only_a_platform_admin_may_sync(policy, community):
    """
    @verifies REQ-0009
    @verifies REQ-0030
    """
    user = operator(roles=(PLATFORM_ADMIN_ROLE,))
    assert policy.allow(user, SYNC, organization=community).allowed


@pytest.mark.parametrize("tier", sorted(TIERS))
def test_no_organization_group_grants_the_sync(policy, tier):
    """An organization's own `admins` administer its REC, not its registry areas.

    @verifies REQ-0009
    """
    decision = policy.allow(operator(org=ORG, groups=(tier,)), SYNC, organization=ORG)
    assert not decision.allowed


@pytest.mark.parametrize("tier", sorted(TIERS))
def test_no_realm_group_grants_the_sync(policy, tier):
    """A realm `admins` group, the platform admin before the role, included.

    @verifies REQ-0009
    @verifies REQ-0030
    """
    assert not policy.allow(operator(realm_groups=(tier,)), SYNC, organization=ORG).allowed


@pytest.mark.parametrize("community", [ORG, OTHER_ORG, None])
@pytest.mark.parametrize(
    "scopes", [("onboarding.admin",), ("onboarding.recs.write",), ("onboarding.*",)]
)
def test_a_service_holding_the_sync_scope_may_sync(policy, scopes, community):
    """The platform operator's client, `celine-cli`, holds `onboarding.admin`.

    @verifies REQ-0009
    """
    decision = policy.allow(service(*scopes), SYNC, organization=community)
    assert decision.allowed
    assert decision.reason == "granted by service scope"


def test_the_operator_client_as_a_realm_issues_it_may_sync(policy):
    """`celine-cli`'s token as a realm synced by celine-policies issues it: `azp`
    and the `trrtcc:` grant marker, no `client_id`, no `preferred_username`, no
    realm role.

    @verifies REQ-0009
    """
    cli = JwtUser(
        sub="5c0e7d2a-9b41-4f6e-8a13-6d2f0b9e7c48",
        claims={
            "azp": "celine-cli",
            "jti": "trrtcc:0f4b8e21-6a7d-4c39-9e52-1b8d3f6a0c74",
            "scope": "digital-twin.admin rec-registry.admin onboarding.admin",
        },
    )
    assert cli.is_service_account
    assert policy.allow(cli, SYNC, organization=ORG).allowed


@pytest.mark.parametrize(
    "scopes",
    [
        (),
        ("onboarding.recs.read",),
        ("onboarding.submissions.review", "onboarding.export"),
        ("onboarding.members.invite", "onboarding.members.release"),
        ("rec-registry.admin",),
    ],
)
def test_a_service_without_the_sync_scope_may_not(policy, scopes):
    """
    @verifies REQ-0009
    """
    decision = policy.allow(service(*scopes), SYNC, organization=ORG)
    assert not decision.allowed
    assert decision.reason == "service is missing a scope granting this action"


@pytest.mark.parametrize(
    "scopes", [("onboarding.members.invite",), ("onboarding.members.release",)]
)
def test_a_delegated_service_call_may_not_sync(policy, scopes):
    """The sync is not a delegated action: an acting platform admin's token lends
    a service nothing, so a delegated scope does not reach it.

    @verifies REQ-0009
    """
    actor = operator(roles=(PLATFORM_ADMIN_ROLE,))
    assert not policy.allow(service(*scopes), SYNC, organization=ORG, actor=actor).allowed


def test_an_org_admin_is_told_why_the_sync_is_refused(policy):
    """
    @verifies REQ-0009
    """
    decision = policy.allow(operator(org=ORG, groups=("admins",)), SYNC, organization=ORG)
    assert (
        decision.reason
        == "only the platform-admin role or a platform operator's client grants this action"
    )


# ---------------------------------------------------------------------------
# recs.drift — the drift check, the REC's managers and admins and the platform admin
# ---------------------------------------------------------------------------

DRIFT_CHECK = Capability.RECS_DRIFT


@pytest.mark.parametrize("tier", ["admins", "managers"])
def test_the_recs_managers_and_admins_may_check_drift(policy, tier):
    """
    @verifies REQ-0015
    """
    decision = policy.allow(operator(org=ORG, groups=(tier,)), DRIFT_CHECK, organization=ORG)
    assert decision.allowed


@pytest.mark.parametrize("tier", ["editors", "viewers"])
def test_the_recs_editors_and_viewers_may_not_check_drift(policy, tier):
    """D55: not organisation viewers.

    @verifies REQ-0015
    """
    decision = policy.allow(operator(org=ORG, groups=(tier,)), DRIFT_CHECK, organization=ORG)
    assert not decision.allowed


def test_another_recs_manager_may_not_check_drift(policy):
    """
    @verifies REQ-0015
    """
    user = operator(org=OTHER_ORG, groups=("managers",))
    assert not policy.allow(user, DRIFT_CHECK, organization=ORG).allowed


@pytest.mark.parametrize("community", [ORG, OTHER_ORG])
def test_a_platform_admin_may_check_drift_everywhere(policy, community):
    """
    @verifies REQ-0015
    @verifies REQ-0030
    """
    user = operator(roles=(PLATFORM_ADMIN_ROLE,))
    assert policy.allow(user, DRIFT_CHECK, organization=community).allowed


@pytest.mark.parametrize("tier", sorted(TIERS))
def test_no_realm_group_may_check_drift(policy, tier):
    """
    @verifies REQ-0015
    @verifies REQ-0030
    """
    user = operator(realm_groups=(tier,))
    assert not policy.allow(user, DRIFT_CHECK, organization=ORG).allowed


def test_a_manager_of_the_rec_with_a_legacy_realm_group_may(policy):
    """The organization grant stands on its own; the realm group adds nothing.

    @verifies REQ-0015
    """
    user = operator(org=ORG, groups=("managers",), realm_groups=("managers",))
    assert policy.allow(user, DRIFT_CHECK, organization=ORG).allowed


@pytest.mark.parametrize(
    "scopes", [("onboarding.admin",), ("onboarding.recs.read",), ("onboarding.*",)]
)
def test_no_scope_grants_the_drift_check(policy, scopes):
    """
    @verifies REQ-0015
    """
    decision = policy.allow(service(*scopes), DRIFT_CHECK, organization=ORG)
    assert not decision.allowed
    assert "people only" in (decision.reason or "")


# ---------------------------------------------------------------------------
# Delegated — a service acting for a verified operator
# ---------------------------------------------------------------------------

INVITE = Capability.MEMBERS_INVITE


def community_service(*scopes: str) -> JwtUser:
    return service(*scopes or ("onboarding.members.invite",))


def test_a_service_acting_for_a_manager_of_the_rec_is_allowed(policy):
    decision = policy.allow(
        community_service(),
        INVITE,
        organization=ORG,
        actor=operator(org=ORG, groups=("managers",)),
    )
    assert decision.allowed
    assert decision.reason == "granted by service scope, acting for an operator"


def test_an_org_admin_actor_is_allowed(policy):
    actor = operator(org=ORG, groups=("admins",))
    assert policy.allow(community_service(), INVITE, organization=ORG, actor=actor).allowed


def test_acting_for_a_manager_of_another_organization_is_denied(policy):
    decision = policy.allow(
        community_service(),
        INVITE,
        organization=ORG,
        actor=operator(org=OTHER_ORG, groups=("managers",)),
    )
    assert not decision.allowed
    assert decision.reason == "the acting operator holds no group granting this action"


def test_a_platform_admin_actor_is_allowed(policy):
    """
    @verifies REQ-0030
    """
    actor = operator(roles=(PLATFORM_ADMIN_ROLE,))
    assert policy.allow(community_service(), INVITE, organization=ORG, actor=actor).allowed


@pytest.mark.parametrize("tier", sorted(TIERS))
def test_a_realm_group_actor_is_denied(policy, tier):
    """
    @verifies REQ-0030
    """
    actor = operator(realm_groups=(tier,))
    decision = policy.allow(community_service(), INVITE, organization=ORG, actor=actor)
    assert not decision.allowed
    assert decision.reason == "the acting operator holds no group granting this action"


@pytest.mark.parametrize("tier", READ_ONLY_TIERS)
def test_an_org_editor_or_viewer_actor_is_denied(policy, tier):
    actor = operator(org=ORG, groups=(tier,))
    assert not policy.allow(community_service(), INVITE, organization=ORG, actor=actor).allowed


def test_an_actor_in_an_untyped_organization_is_denied(policy):
    actor = operator(org=ORG, groups=("managers",), org_type=None)
    assert not policy.allow(community_service(), INVITE, organization=ORG, actor=actor).allowed


def test_a_service_with_the_scope_and_no_actor_is_denied(policy):
    decision = policy.allow(community_service(), INVITE, organization=ORG)
    assert not decision.allowed
    assert decision.reason == "a delegated action needs an acting operator"


def test_a_service_actor_is_not_an_operator(policy):
    """Another service's token in the actor slot names nobody's decision."""
    decision = policy.allow(
        community_service(), INVITE, organization=ORG, actor=service("onboarding.admin")
    )
    assert not decision.allowed
    assert decision.reason == "a delegated action needs an acting operator"


def test_onboarding_admin_needs_an_actor_too(policy):
    admin = service("onboarding.admin")
    manager = operator(org=ORG, groups=("managers",))
    assert policy.allow(admin, INVITE, organization=ORG, actor=manager).allowed
    assert not policy.allow(admin, INVITE, organization=ORG).allowed


def test_a_service_without_the_scope_is_denied_even_with_an_actor(policy):
    decision = policy.allow(
        service("onboarding.submissions.review"),
        INVITE,
        organization=ORG,
        actor=operator(org=ORG, groups=("managers",)),
    )
    assert not decision.allowed
    assert "missing a scope" in (decision.reason or "")


@pytest.mark.parametrize(
    "caller",
    [
        operator(org=ORG, groups=("managers",)),
        operator(org=ORG, groups=("admins",)),
        operator(roles=(PLATFORM_ADMIN_ROLE,)),
    ],
    ids=["org-manager", "org-admin", "platform-admin"],
)
def test_a_managers_own_token_is_denied(policy, caller):
    """The dashboard is the one path, so the community's own audit row always exists."""
    decision = policy.allow(caller, INVITE, organization=ORG)
    assert not decision.allowed
    assert "only through a service" in (decision.reason or "")


def test_an_operator_acting_for_themselves_is_still_denied(policy):
    manager = operator(org=ORG, groups=("managers",))
    assert not policy.allow(manager, INVITE, organization=ORG, actor=manager).allowed


@pytest.mark.parametrize("tier", sorted(TIERS))
def test_no_operator_lists_the_delegated_capability(policy, tier):
    user = operator(org=ORG, groups=(tier,), roles=(PLATFORM_ADMIN_ROLE,), realm_groups=(tier,))
    assert DELEGATED.isdisjoint(policy.capabilities(user, organization=ORG))


def test_the_actor_does_not_widen_a_non_delegated_action(policy):
    """A service without a scope stays denied, whoever it claims to act for."""
    decision = policy.allow(
        community_service(),
        Capability.SUBMISSIONS_PURGE,
        organization=ORG,
        actor=operator(roles=(PLATFORM_ADMIN_ROLE,)),
    )
    assert not decision.allowed


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_unknown_capability_is_denied(policy):
    decision = policy.allow(
        operator(roles=(PLATFORM_ADMIN_ROLE,)), "submissions.teleport", organization=ORG
    )
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

    platform_decision = policy.allow(
        operator(roles=(PLATFORM_ADMIN_ROLE,)), Capability.SUBMISSIONS_PURGE, organization=ORG
    )
    assert platform_decision.allowed
    assert platform_decision.reason == "granted by platform role"


# ---------------------------------------------------------------------------
# Unloadable bundle
# ---------------------------------------------------------------------------


def test_missing_policy_bundle_denies_by_default(tmp_path):
    broken = OnboardingAccessPolicy(policies_dir=tmp_path / "nope")
    assert not broken.available
    decision = broken.allow(
        operator(roles=(PLATFORM_ADMIN_ROLE,)), Capability.SUBMISSIONS_READ, organization=ORG
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
# Delegated release — REC admins only (requester, 2026-10-05)
# ---------------------------------------------------------------------------

RELEASE = Capability.MEMBERS_RELEASE


def release_service() -> JwtUser:
    return service("onboarding.members.release")


def test_a_service_acting_for_a_rec_admin_may_release(policy):
    actor = operator(org=ORG, groups=("admins",))
    decision = policy.allow(release_service(), RELEASE, organization=ORG, actor=actor)
    assert decision.allowed


def test_a_service_acting_for_the_platform_admin_may_release(policy):
    actor = operator(roles=(PLATFORM_ADMIN_ROLE,))
    assert policy.allow(release_service(), RELEASE, organization=ORG, actor=actor).allowed


@pytest.mark.parametrize("tier", ["managers", "editors", "viewers"])
def test_no_tier_below_admins_may_release(policy, tier):
    actor = operator(org=ORG, groups=(tier,))
    decision = policy.allow(release_service(), RELEASE, organization=ORG, actor=actor)
    assert not decision.allowed
    assert decision.reason == "the acting operator holds no group granting this action"


def test_an_admin_of_another_rec_may_not_release(policy):
    actor = operator(org=OTHER_ORG, groups=("admins",))
    assert not policy.allow(release_service(), RELEASE, organization=ORG, actor=actor).allowed


def test_the_invite_scope_does_not_release(policy):
    actor = operator(org=ORG, groups=("admins",))
    decision = policy.allow(community_service(), RELEASE, organization=ORG, actor=actor)
    assert not decision.allowed
    assert "missing a scope" in (decision.reason or "")


def test_an_admins_own_token_may_not_release(policy):
    admin = operator(org=ORG, groups=("admins",))
    decision = policy.allow(admin, RELEASE, organization=ORG)
    assert not decision.allowed
    assert "only through a service" in (decision.reason or "")
