"""OPA access policy for the onboarding admin console.

Wraps `policies/celine/onboarding/access.rego`, evaluated in-process through
`celine.sdk.policies.PolicyEngine.evaluate_decision` — the high-level API that
builds proper ``data.{package}.allow`` / ``.reason`` queries rather than
evaluating the package path as a raw Rego expression.

The wrapper's one job beyond plumbing is to keep realm-level and
organization-level groups **apart** — a realm group is a platform-wide grant, so
merging the two levels would let a `managers` badge held inside community A
authorise an action on community B. The readers that keep them apart
(`celine.sdk.auth.realm_groups`, `JwtUser.get_organization`) live in the SDK;
this module used to carry private copies of them.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from celine.sdk.auth import JwtUser, realm_groups

from celine.onboarding.config.settings import settings

logger = logging.getLogger(__name__)

_PACKAGE = "celine.onboarding.access"


class Capability(enum.StrEnum):
    """What a caller may do, as named in `access.rego`'s capability tables.

    These strings are the contract with the policy: an action the rego does not
    know is denied, so a typo here fails closed rather than open.
    """

    RECS_READ = "recs.read"
    SUBMISSIONS_READ = "submissions.read"
    SUBMISSIONS_REVEAL = "submissions.reveal"
    SUBMISSIONS_WRITE = "submissions.write"
    SUBMISSIONS_REVIEW = "submissions.review"
    SUBMISSIONS_PURGE = "submissions.purge"
    ENABLEMENT_RETRY = "enablement.retry"
    ENABLEMENT_REVOKE = "enablement.revoke"
    AUDIT_READ = "audit.read"
    EXPORT = "export"


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
    ) -> Decision:
        """Decide whether *user* may perform *capability* on *organization*'s REC."""
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
            policy_input = self._policy_input(user, action, organization)
            result = self._engine.evaluate_decision(_PACKAGE, policy_input)
            decision = Decision(allowed=bool(result.allowed), reason=result.reason or None)
        except Exception as exc:
            # Fail closed. grid answers permissively here; a policy that cannot be
            # evaluated is indistinguishable from one that would have denied, and
            # this surface can approve people and erase them.
            logger.exception("Policy evaluation failed for %s: %s", action, exc)
            return Decision(False, "authorization error")

        if decision.allowed:
            logger.debug(
                "Allowed sub=%s action=%s org=%s reason=%s",
                user.sub,
                action,
                organization,
                decision.reason,
            )
        else:
            logger.warning(
                "Denied sub=%s action=%s org=%s reason=%s",
                user.sub,
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

    def _policy_input(self, user: JwtUser, action: str, organization: str | None):
        from celine.sdk.policies import (
            Action,
            PolicyInput,
            Resource,
            ResourceType,
            Subject,
            SubjectType,
        )

        claims = user.claims or {}
        aliases = user.organization_aliases
        realm = realm_groups(claims)

        # Prefer organization/group presence as the authoritative signal for
        # "this is a human". `is_service_account()` can misfire on a user JWT
        # that carries a `scope` claim but no `groups` — the same trap
        # celine-grid documents.
        if aliases or realm:
            subject_type = SubjectType.USER
        elif user.is_service_account:
            subject_type = SubjectType.SERVICE
        else:
            subject_type = SubjectType.USER

        scope_claim = claims.get("scope") or ""
        scopes = scope_claim.split() if isinstance(scope_claim, str) else list(scope_claim)

        # Only the organization matching *this* request is passed through, along
        # with that organization's groups and type. The rego therefore cannot
        # compare a group from one community against another community's REC,
        # and it can ask what kind of organization this is: the console
        # administers RECs, so one the realm does not type as a REC authorises
        # nothing here — see `granted_by_org_group` in the rego.
        #
        # `JwtUser.organizations` is parsed by `from_token`, which is the only
        # way this service ever builds one. The SDK reads the organization's
        # attributes flattened-first, which is the shape a real Keycloak token
        # carries; a policy written against `attributes.type` matches nothing.
        matched_org = user.get_organization(organization) if organization else None
        matched = matched_org.alias if matched_org else None
        org_groups = matched_org.groups if matched_org else []
        org_type = matched_org.type if matched_org else None

        return PolicyInput(
            subject=Subject(
                id=user.sub,
                type=subject_type,
                groups=realm,
                scopes=scopes,
                claims={
                    "organization": matched,
                    "org_groups": org_groups,
                    "org_type": org_type,
                },
            ),
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


@lru_cache(maxsize=1)
def get_policy() -> OnboardingAccessPolicy:
    """Process-wide policy singleton.

    Lazy rather than module-level so that importing this module does not read the
    filesystem, and so tests can rebuild it with `get_policy.cache_clear()`.
    """
    return OnboardingAccessPolicy()
