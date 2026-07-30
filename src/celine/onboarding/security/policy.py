"""OPA access policy for the onboarding admin console.

Wraps `policies/celine/onboarding/access.rego`, evaluated in-process through
`celine.sdk.policies.PolicyEngine.evaluate_decision` — the high-level API that
builds proper ``data.{package}.allow`` / ``.reason`` queries rather than
evaluating the package path as a raw Rego expression.

The wrapper's one job beyond plumbing is to keep realm-level and
organization-level groups **apart**. See `realm_groups` for why.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from celine.sdk.auth import JwtUser

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
# Claim readers
# ---------------------------------------------------------------------------


def _normalize(values: Any) -> list[str]:
    """Strip Keycloak's leading slash and deduplicate, preserving order.

    Anything that is not a list is discarded rather than iterated: a `groups`
    claim that arrived as a bare string would otherwise be walked character by
    character and yield single-letter "groups". `extract_groups` in the SDK
    guards the same way.
    """
    if not isinstance(values, (list, tuple)):
        return []

    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        name = value.lstrip("/")
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def realm_groups(claims: dict[str, Any]) -> list[str]:
    """Groups from the top-level ``groups`` claim — realm level **only**.

    Deliberately not `celine.sdk.auth.jwt.extract_groups`, which merges realm
    groups with every organization's groups into one flat list. That is the right
    behaviour for a service asking "is this user a viewer?", and the wrong
    behaviour here: a realm group is a platform-wide grant, so merging would let
    a `managers` badge held inside community A authorise an action on community
    B. The console needs the two levels distinguishable, so it reads them apart.
    """
    return _normalize(claims.get("groups"))


def organization_groups(claims: dict[str, Any], alias: str) -> list[str]:
    """Groups the caller holds inside one specific organization."""
    orgs = claims.get("organization")
    if not isinstance(orgs, dict):
        return []
    org = orgs.get(alias)
    if not isinstance(org, dict):
        return []
    return _normalize(org.get("groups"))


def organization_aliases(claims: dict[str, Any]) -> list[str]:
    """Every organization alias the caller is a member of."""
    orgs = claims.get("organization")
    if not isinstance(orgs, dict):
        return []
    return sorted(str(alias) for alias in orgs)


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
        aliases = organization_aliases(claims)
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
        # with that organization's groups. The rego therefore cannot compare a
        # group from one community against another community's REC.
        matched = organization if organization and organization in aliases else None
        org_groups = organization_groups(claims, matched) if matched else []

        return PolicyInput(
            subject=Subject(
                id=user.sub,
                type=subject_type,
                groups=realm,
                scopes=scopes,
                claims={"organization": matched, "org_groups": org_groups},
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
