"""OPA access policy for the onboarding admin console.

Wraps `policies/celine/onboarding/access.rego`, evaluated in-process by
`celine.sdk.policies.PolicyEngine`, querying ``data.{package}.allow`` and
``.reason``.

The wrapper's one job beyond plumbing is to keep the two levels of grant
**apart** (REQ-0030). The platform level is the realm role `platform-admin`,
passed as `input.subject.roles`; the organization level is the groups held inside
the one organization the request concerns. A realm group (the top-level `groups`
claim) is never read: an organization's groups carry the same names, so reading
both would let a community's own `admins` act as the platform's. The readers
(`JwtUser.realm_roles`, `JwtUser.get_organization`) are the SDK's.

The input is the SDK's: a `PolicyInput` serialised by
`PolicyEngine.build_input_dict`, which emits `input.subject.roles` beside
`input.subject.groups` and never merges the two (celine-sdk 2.0.0). Until that
release this module built the mapping by hand, because the SDK's `Subject` then
had no `roles` field and dropped one silently.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from celine.sdk.auth import JwtUser

from celine.onboarding.config.settings import settings

if TYPE_CHECKING:
    from celine.sdk.policies import Subject

logger = logging.getLogger(__name__)

_PACKAGE = "celine.onboarding.access"


class Capability(enum.StrEnum):
    """What a caller may do, as named in `access.rego`'s capability tables.

    These strings are the contract with the policy: an action the rego does not
    know is denied, so a typo here fails closed rather than open.
    """

    RECS_READ = "recs.read"
    #: The platform operator only: the `platform-admin` role, or the scope
    #: `onboarding.recs.write` (`onboarding.admin` covers it); never an organization
    #: group. The registry sync of a REC's template areas. See
    #: `platform_only_actions` in the rego.
    RECS_WRITE = "recs.write"
    #: The console's drift check of a REC's registry areas (D55): the
    #: `platform-admin` role, and that REC's own `managers` and `admins`; no
    #: scope. See `people_only_actions` in the rego.
    RECS_DRIFT = "recs.drift"
    SUBMISSIONS_READ = "submissions.read"
    SUBMISSIONS_REVEAL = "submissions.reveal"
    SUBMISSIONS_WRITE = "submissions.write"
    SUBMISSIONS_REVIEW = "submissions.review"
    #: Correcting a POD, name or email from `submitted` on, as a tracked revision
    #: (`services/revision.py`). Granted where `submissions.review` is: the
    #: operator vouches for the new value, which is part of the decision.
    SUBMISSIONS_REVISE = "submissions.revise"
    SUBMISSIONS_PURGE = "submissions.purge"
    ENABLEMENT_RETRY = "enablement.retry"
    ENABLEMENT_REVOKE = "enablement.revoke"
    AUDIT_READ = "audit.read"
    EXPORT = "export"
    #: Delegated: allowed only to a service acting for a verified operator, so a
    #: person evaluating it alone is always denied and `/api/admin/me` never lists
    #: it. See `delegated_actions` in the rego.
    MEMBERS_INVITE = "members.invite"
    #: Delegated like `members.invite`, and REC `admins` only: the release of a
    #: registry member, which the community dashboard asks for on a REC admin's
    #: behalf (`api/admin/members.py`).
    MEMBERS_RELEASE = "members.release"


ALL_CAPABILITIES: tuple[Capability, ...] = tuple(Capability)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None = None


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class OnboardingAccessPolicy:
    """Evaluates `celine.onboarding.access` for one (caller, action, REC) triple.

    Loaded once; decisions are evaluated per request. When the bundle cannot be
    loaded the policy **denies** unless `ALLOW_PERMISSIVE_POLICY=true`, which is
    a deliberate departure from celine-grid: the condition being papered over is
    "no authorization at all".
    """

    def __init__(self, policies_dir: str | Path | None = None) -> None:
        self._engine: Any = None
        self._load_error: str | None = None

        directory = Path(policies_dir or settings.policies_dir)
        try:
            from celine.sdk.policies import PolicyEngine

            if not directory.exists():
                self._load_error = f"policies directory not found: {directory}"
            else:
                engine = PolicyEngine(policies_dir=str(directory))
                engine.load()
                self._engine = engine
                logger.info("Onboarding access policy loaded from %s", directory)
        except ImportError as exc:  # pragma: no cover - packaging accident
            self._load_error = f"celine.sdk.policies unavailable: {exc}"
        except Exception as exc:
            self._load_error = f"policy bundle failed to load: {exc}"

        if self._load_error:
            logger.error(
                "Onboarding access policy unavailable — %s (permissive=%s)",
                self._load_error,
                settings.allow_permissive_policy,
            )

    @property
    def available(self) -> bool:
        return self._engine is not None

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def allow(
        self,
        user: JwtUser,
        capability: Capability | str,
        *,
        organization: str | None,
        actor: JwtUser | None = None,
    ) -> Decision:
        """Decide whether *user* may perform *capability* on *organization*'s REC.

        *actor* is the operator a service acts for, from their own verified token.
        Only a delegated capability reads it; every other one ignores it.
        """
        action = capability.value if isinstance(capability, Capability) else str(capability)

        if self._engine is None:
            if settings.allow_permissive_policy:
                logger.warning(
                    "Permissive policy fallback allowed %s on org=%s (%s)",
                    action,
                    organization,
                    self._load_error,
                )
                return Decision(True, "policy-engine-unavailable-permissive")
            return Decision(False, "authorization unavailable")

        try:
            policy_input = self._policy_input(user, action, organization, actor=actor)
            allowed = _value(self._engine.evaluate(f"data.{_PACKAGE}.allow", policy_input))
            reason = _value(self._engine.evaluate(f"data.{_PACKAGE}.reason", policy_input))
            decision = Decision(
                allowed=allowed is True,
                reason=reason if isinstance(reason, str) and reason else None,
            )
        except Exception as exc:
            # Fail closed. grid answers permissively here; a policy that cannot be
            # evaluated is indistinguishable from one that would have denied, and
            # this surface can approve people and erase them.
            logger.exception("Policy evaluation failed for %s: %s", action, exc)
            return Decision(False, "authorization error")

        acting = actor.sub if actor is not None else None
        if decision.allowed:
            logger.debug(
                "Allowed sub=%s actor=%s action=%s org=%s reason=%s",
                user.sub,
                acting,
                action,
                organization,
                decision.reason,
            )
        else:
            logger.warning(
                "Denied sub=%s actor=%s action=%s org=%s reason=%s",
                user.sub,
                acting,
                action,
                organization,
                decision.reason,
            )
        return decision

    def capabilities(self, user: JwtUser, *, organization: str | None) -> frozenset[str]:
        """Every capability *user* holds on *organization*'s REC.

        Drives `GET /api/admin/me` (so the UI can hide what the operator cannot
        do) and `GET /api/admin/recs` (a REC with no capabilities is not listed).
        """
        return frozenset(
            capability.value
            for capability in ALL_CAPABILITIES
            if self.allow(user, capability, organization=organization).allowed
        )

    # -- input construction ------------------------------------------------

    def _policy_input(
        self,
        user: JwtUser,
        action: str,
        organization: str | None,
        *,
        actor: JwtUser | None = None,
    ) -> dict[str, Any]:
        from celine.sdk.policies import Action, PolicyInput, Resource, ResourceType

        policy_input = PolicyInput(
            subject=_subject(user, organization),
            resource=Resource(
                # USERDATA is a generic stand-in: access.rego inspects only
                # resource.attributes, never resource.type, and the SDK's
                # ResourceType enum has no onboarding member.
                type=ResourceType.USERDATA,
                id=f"onboarding/{organization or '-'}",
                attributes={"organization": organization},
            ),
            action=Action(name=action),
        )

        # The acting operator goes in `environment`, the policy input's free-form
        # request slot. It is built and serialised by the same code as the
        # subject, so the rego judges it by the same rules and it cannot carry a
        # group from another community either.
        if actor is not None:
            acting = policy_input.model_copy(update={"subject": _subject(actor, organization)})
            policy_input.environment["actor"] = self._engine.build_input_dict(acting)["subject"]

        return self._engine.build_input_dict(policy_input)


def _value(result: Any) -> Any:
    """The value of a single-expression query result, or None when undefined."""
    try:
        return result["result"][0]["expressions"][0]["value"]
    except (KeyError, IndexError, TypeError):
        return None


def _subject(user: JwtUser, organization: str | None) -> Subject:
    """The policy's view of one principal, against one REC's organization.

    The two levels of grant travel apart (REQ-0030): the realm roles in `roles`,
    the matched organization's groups in `claims.org_groups`, and `groups` always
    empty — a realm group is not read, so it cannot grant anything.
    """
    from celine.sdk.policies import Subject, SubjectType

    claims = user.claims or {}

    # Person or service is the SDK's call, from the token's own markers. A realm
    # group is not consulted for it: a group is not a grant, and since the realm
    # roles moved out of groups it would decide nothing a person's token does not
    # already say.
    subject_type = SubjectType.SERVICE if user.is_service_account else SubjectType.USER

    scope_claim = claims.get("scope") or ""
    scopes = scope_claim.split() if isinstance(scope_claim, str) else list(scope_claim)

    # Only the organization matching *this* request is passed through, along
    # with that organization's groups and type. The rego therefore cannot
    # compare a group from one community against another community's REC,
    # and it can ask what kind of organization this is: the console
    # administers RECs, so one the realm does not type as a REC authorises
    # nothing here — see `org_group_grants` in the rego.
    #
    # `JwtUser.organizations` is parsed by `from_token`, which is the only
    # way this service ever builds one. The SDK reads the organization's
    # attributes flattened-first, which is the shape a real Keycloak token
    # carries; a policy written against `attributes.type` matches nothing.
    matched_org = user.get_organization(organization) if organization else None
    matched = matched_org.alias if matched_org else None
    org_groups = list(matched_org.groups) if matched_org else []
    org_type = matched_org.type if matched_org else None

    return Subject(
        id=user.sub,
        type=subject_type,
        roles=list(user.realm_roles),
        groups=[],
        scopes=scopes,
        claims={
            "organization": matched,
            "org_groups": org_groups,
            "org_type": org_type,
        },
    )


@lru_cache(maxsize=1)
def get_policy() -> OnboardingAccessPolicy:
    """Process-wide policy singleton.

    Lazy rather than module-level so that importing this module does not read the
    filesystem, and so tests can rebuild it with `get_policy.cache_clear()`.
    """
    return OnboardingAccessPolicy()
