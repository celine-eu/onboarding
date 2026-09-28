"""Pushing a template's areas to the REC registry, on a platform admin's request.

The template is the source of truth for a community's areas (ADR-0012). Each
area is one GSE primary-substation boundary, and the registry holds it as one
area ``{name, boundary: {source, id}, topology: [<id>]}`` plus one topology node
``{id, type: primary_substation, name}`` with the same id; ``name`` is the
template's display name for the area, or its key (D57). This module writes
exactly that, and only when :func:`sync` is called — never on a template load or
reload (REQ-0009).

The order of one run:

1. **Validate the template as it is now** (REQ-0013): the template import's
   structural checks, then every boundary id against the Digital Twin. A
   template that fails is not synced and nothing is called after.
2. **Read the registry community** with this service's default token
   (``rec-registry.read``) and **plan**: what to create, change, leave alone,
   refuse, and which registry areas the template no longer declares.
3. **Set the community up** (REQ-0014, not in a dry run): the provisioning
   service's ``POST /reconcile/{community}``, with a token asking for the
   optional ``provisioning.reconcile``. A failure or a missing
   ``PROVISIONING_URL`` is reported and does not stop what follows (ADR-0014).
4. **Write** (not in a dry run), with a token asking for the optional
   ``rec-registry.community.write``: nodes first — an area needs its node — then
   the renames (a template area whose boundary the registry holds under a key
   the template no longer declares moves to its new key, with its members, in
   one registry request: D54), then, with ``prune=true``, the undeclared areas
   the registry lets go, then the areas, then the nodes only a pruned area used.

**Nothing personal passes through here**, and nothing is logged beyond keys,
boundary ids and counts: area keys are hand-made names, boundary ids are public
substation codes, and the member count of an area is a number. The members an
area holds are counted, never read out.

The registry calls go through ``celine.sdk.rec_registry``'s
:class:`RecRegistryAdminClient` (celine-sdk 1.21.0, D59): the community read and
the area, topology node and rename writes, each raising the registry's refusal
with its ``code`` (``{detail, code}``). Only the member count is sent by hand.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
from celine.sdk.rec_registry import RecRegistryAdminClient, RecRegistryApiError

from celine.onboarding.config.settings import settings
from celine.onboarding.services import template_service
from celine.onboarding.services.template_service import BoundaryRef, RecRegistryBinding

logger = logging.getLogger(__name__)

#: The optional scope asked for, for the writes only (D37).
COMMUNITY_WRITE_SCOPE = "rec-registry.community.write"
#: The optional scope asked for, for the set-up step only (D37).
RECONCILE_SCOPE = "provisioning.reconcile"

PRIMARY_SUBSTATION = "primary_substation"

#: The registry's page limit on a member listing.
_MEMBER_PAGE = 500

# Outcomes. In a dry run they say what a real run would do.
CREATED = "created"
CHANGED = "changed"
UNCHANGED = "unchanged"
REFUSED = "refused"
DELETED = "deleted"
#: A registry area the template does not declare, left in place (no ``prune``).
UNDECLARED = "undeclared"
#: A registry area moved to the template's key, with its members (D54).
RENAMED = "renamed"
#: Not attempted, because the registry stopped answering earlier in the run.
NOT_RUN = "not_run"


class SyncRefusedError(Exception):
    """The sync did not run: nothing was written anywhere.

    ``status_code`` and ``code`` are the route's answer.
    """

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


class RegistryRefusalError(Exception):
    """The registry answered a refusal: its status, its ``code`` and its sentence."""

    def __init__(self, status_code: int, code: str | None, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


class RegistryUnavailableError(Exception):
    """The registry did not answer at all. Never carries a body."""


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass
class Item:
    """One area or one topology node, and what the run did (or would do) to it."""

    key: str
    outcome: str
    boundary_id: str | None = None
    code: str | None = None
    reason: str | None = None
    #: For an area the template no longer declares: how many members it holds.
    #: For a renamed area: how many members moved with it (in a dry run, how
    #: many would).
    members: int | None = None
    #: For a renamed area: the registry key it was moved from (D54).
    renamed_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "boundary_id": self.boundary_id,
            "outcome": self.outcome,
            "code": self.code,
            "reason": self.reason,
            "members": self.members,
            "renamed_from": self.renamed_from,
        }


@dataclass
class SetupStep:
    """The community's set-up through the provisioning reconcile (REQ-0014).

    ``status`` is ``succeeded``, ``failed``, ``skipped`` (no provisioning
    service configured) or ``not_run`` (a dry run).
    """

    status: str
    code: str | None = None
    reason: str | None = None
    members: int | None = None
    created: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "reason": self.reason,
            "members": self.members,
            "created": self.created,
        }


@dataclass
class SyncReport:
    rec: str
    community: str
    dry_run: bool
    prune: bool
    setup: SetupStep
    nodes: list[Item] = field(default_factory=list)
    areas: list[Item] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Every area and node written or left alone as planned."""
        return not any(i.outcome in (REFUSED, NOT_RUN) for i in self.nodes + self.areas)

    def counts(self, items: list[Item]) -> dict[str, int]:
        found: dict[str, int] = {}
        for item in items:
            found[item.outcome] = found.get(item.outcome, 0) + 1
        return found

    def to_dict(self) -> dict[str, Any]:
        return {
            "rec": self.rec,
            "community": self.community,
            "dry_run": self.dry_run,
            "prune": self.prune,
            "ok": self.ok,
            "setup": self.setup.to_dict(),
            "nodes": [i.to_dict() for i in self.nodes],
            "areas": [i.to_dict() for i in self.areas],
            "summary": {"nodes": self.counts(self.nodes), "areas": self.counts(self.areas)},
        }

    def audit_detail(self) -> str:
        """Counts only: what the trail keeps of a sync."""
        areas = self.counts(self.areas)
        nodes = self.counts(self.nodes)
        parts = [f"community={self.community}", f"prune={str(self.prune).lower()}"]
        parts += [f"areas_{k}={v}" for k, v in sorted(areas.items())]
        parts += [f"nodes_{k}={v}" for k, v in sorted(nodes.items())]
        parts.append(f"setup={self.setup.status}")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


async def _token(scope: str | None = None) -> str:
    """This service's own token, ``svc-onboarding``, with *scope* asked for on top.

    The default token reads; the writes ask for ``rec-registry.community.write``
    for their own calls only.
    """
    from celine.onboarding.services.service_auth import celine_token_provider

    token = await celine_token_provider(scope).get_token()
    return token.access_token


def _refusal(exc: RecRegistryApiError) -> RegistryRefusalError:
    """The registry's ``{detail, code}`` as the SDK read it, leniently."""
    status = exc.status_code or 0
    detail = exc.detail if isinstance(exc.detail, str) else f"rec-registry answered {status}"
    return RegistryRefusalError(status, exc.code, detail)


class RegistryAreas:
    """The registry routes a sync needs, one community at a time.

    The SDK's :class:`RecRegistryAdminClient` makes the calls and quotes their
    paths; each call carries a token minted for it, asking for the optional
    scope that call needs (D37). The SDK raises :class:`RecRegistryApiError`
    for anything but ``200``, which becomes :class:`RegistryRefusalError`; a
    registry that does not answer, or answers ``200`` with nothing readable,
    becomes :class:`RegistryUnavailableError`.

    The member count is the one call made by hand: the SDK's member listing
    parses every member it reads, and this sync only counts them.
    """

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._admin = RecRegistryAdminClient(base_url=self._base, timeout=timeout)
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _sdk(self, call, *args: Any, scope: str | None = None) -> Any:
        """One SDK call with a token asking for *scope*, its errors mapped."""
        token = await _token(scope)
        try:
            return await call(*args, token=token)
        except RecRegistryApiError as exc:
            if exc.status_code is None or exc.status_code < 400:
                # A 200 the SDK could not read.
                raise RegistryUnavailableError("an answer that is not readable") from None
            raise _refusal(exc) from None
        except httpx.HTTPError as exc:
            raise RegistryUnavailableError(type(exc).__name__) from None

    async def read_community(self, community: str) -> dict[str, Any]:
        answer = await self._sdk(self._admin.read_community, community)
        return answer.model_dump(mode="json")

    async def count_members(self, community: str, area: str) -> int:
        """How many members name *area*, counted page by page; nothing else is kept."""
        url = f"{self._base}/admin/communities/{quote(community, safe='')}/members"
        count = 0
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"area": area, "limit": _MEMBER_PAGE}
            if cursor:
                params["cursor"] = cursor
            headers = {"Authorization": f"Bearer {await _token()}"}
            try:
                response = await self._client.get(url, headers=headers, params=params)
            except httpx.HTTPError as exc:
                raise RegistryUnavailableError(type(exc).__name__) from None
            if response.status_code >= 400:
                code, detail = RecRegistryApiError.refusal_of(response.content)
                raise RegistryRefusalError(
                    response.status_code,
                    code,
                    detail
                    if isinstance(detail, str)
                    else f"rec-registry answered {response.status_code}",
                )
            try:
                page = response.json()
            except ValueError:
                raise RegistryUnavailableError("an answer that is not JSON") from None
            count += len(page.get("items") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                return count

    async def put_node(self, community: str, node_id: str, body: dict[str, Any]) -> None:
        await self._sdk(
            self._admin.put_topology_node, community, node_id, body, scope=COMMUNITY_WRITE_SCOPE
        )

    async def delete_node(self, community: str, node_id: str) -> None:
        await self._sdk(
            self._admin.delete_topology_node, community, node_id, scope=COMMUNITY_WRITE_SCOPE
        )

    async def put_area(self, community: str, area_key: str, body: dict[str, Any]) -> None:
        await self._sdk(
            self._admin.put_area, community, area_key, body, scope=COMMUNITY_WRITE_SCOPE
        )

    async def delete_area(self, community: str, area_key: str) -> None:
        await self._sdk(self._admin.delete_area, community, area_key, scope=COMMUNITY_WRITE_SCOPE)

    async def rename_area(self, community: str, area_key: str, new_key: str) -> int:
        """Move *area_key* to *new_key* with its members, in one request (D54).

        The registry does it in one transaction under the community lock: the
        area as stored, its members' ``area`` and its boundary move together.
        Answers how many members moved, as the registry counts them.
        """
        answer = await self._sdk(
            self._admin.rename_area, community, area_key, new_key, scope=COMMUNITY_WRITE_SCOPE
        )
        return answer.members_moved


def registry() -> RegistryAreas:
    """The registry client for a sync, or :class:`SyncRefusedError` when none is configured."""
    if not settings.rec_registry_url.strip():
        raise SyncRefusedError(
            503,
            "registry_not_configured",
            "REC_REGISTRY_URL is not set, so there is no registry to sync the areas to",
        )
    return RegistryAreas(settings.rec_registry_url)


# ---------------------------------------------------------------------------
# Validation (REQ-0013)
# ---------------------------------------------------------------------------


async def syncable_binding(rec_slug: str) -> RecRegistryBinding:
    """The REC's binding, after every check template import applies.

    Raises :class:`SyncRefusedError`: ``422 template_not_syncable`` for a template
    with no ``rec_registry`` block or no boundary areas, ``422 template_invalid``
    for one that fails a check, ``503 boundaries_unavailable`` when the Digital
    Twin cannot be asked.
    """
    from celine.onboarding.services.boundaries import (
        BoundariesUncheckedError,
        verify_template_boundaries,
    )

    manifest = template_service.load_manifest(rec_slug)
    block = manifest.get("rec_registry")
    where = f"REC {rec_slug!r}"
    if not block:
        raise SyncRefusedError(
            422,
            "template_not_syncable",
            f"{where} declares no 'rec_registry' block, so it has no registry community",
        )
    try:
        template_service.validate_rec_registry_block(block, where=where)
        template_service.validate_boundary_template(manifest, where=where)
    except ValueError as exc:
        raise SyncRefusedError(422, "template_invalid", str(exc)) from None
    if not template_service.declares_boundaries(block):
        raise SyncRefusedError(
            422,
            "template_not_syncable",
            f"{where} declares its areas as municipality lists; only boundary areas "
            "are synced to the registry",
        )

    try:
        await verify_template_boundaries(manifest, where=where)
    except BoundariesUncheckedError as exc:
        raise SyncRefusedError(503, "boundaries_unavailable", str(exc)) from None
    except ValueError as exc:
        raise SyncRefusedError(422, "template_invalid", str(exc)) from None
    return template_service.rec_registry_binding(rec_slug)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def desired_node(name: str, ref: BoundaryRef, existing: dict[str, Any] | None) -> dict:
    """The topology node an area needs, keeping what the template does not own.

    *name* is the area's display name (the template's ``name``, or its key).
    ``operator_id``, ``parent`` and the node's own ``area`` shape are not the
    template's, so a node written again keeps them.
    """
    body: dict[str, Any] = {"id": ref.id, "type": PRIMARY_SUBSTATION, "name": name}
    for key in ("operator_id", "parent", "area"):
        value = (existing or {}).get(key)
        if value:
            body[key] = value
    return body


def desired_area(name: str, ref: BoundaryRef, existing: dict[str, Any] | None) -> dict:
    """The registry area for one template area, keeping what the template does not own.

    *name* is the area's display name (the template's ``name``, or its key).
    ``location`` and ``geometry`` are not the template's: an area written again
    keeps them rather than losing them to a sync.
    """
    body: dict[str, Any] = {
        "name": name,
        "boundary": {"source": ref.source, "id": ref.id},
        "topology": [ref.id],
    }
    for key in ("location", "geometry"):
        value = (existing or {}).get(key)
        if value:
            body[key] = value
    return body


def _node_matches(node: dict[str, Any], name: str) -> bool:
    return node.get("type") == PRIMARY_SUBSTATION and node.get("name") == name


def _area_matches(area: dict[str, Any], name: str, ref: BoundaryRef) -> bool:
    boundary = area.get("boundary") or {}
    return (
        area.get("name") == name
        and boundary.get("source") == ref.source
        and boundary.get("id") == ref.id
        and list(area.get("topology") or []) == [ref.id]
    )


def _boundary_of(area: dict[str, Any]) -> str | None:
    boundary = area.get("boundary")
    if isinstance(boundary, dict) and isinstance(boundary.get("id"), str):
        return boundary["id"]
    return None


@dataclass
class Plan:
    """What a run would do, before it does any of it."""

    #: node id -> (outcome, body), in template order.
    nodes: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    #: area key -> (outcome, body, boundary ref).
    areas: dict[str, tuple[str, dict[str, Any], BoundaryRef]] = field(default_factory=dict)
    #: Registry areas the template does not declare -> their topology node ids.
    undeclared: dict[str, list[str]] = field(default_factory=dict)
    #: area key -> the registry area keys holding its boundary id today.
    held_by: dict[str, list[str]] = field(default_factory=dict)
    #: A renamed area (D54): template key -> the registry key it moves from.
    renames: dict[str, str] = field(default_factory=dict)
    #: The renamed areas that still need their ``PUT`` once moved: the area as
    #: stored differs from what the template wants (its name, say).
    rewrite_after_rename: set[str] = field(default_factory=set)


def plan(binding: RecRegistryBinding, community: dict[str, Any]) -> Plan:
    """Compare the template's areas with the registry community's."""
    areas: dict[str, Any] = community.get("areas") or {}
    topology: list[dict[str, Any]] = community.get("topology") or []
    nodes_by_id = {n.get("id"): n for n in topology if isinstance(n, dict)}

    result = Plan()
    for area_key, ref in sorted(binding.boundaries.items()):
        name = binding.area_name(area_key)
        node = nodes_by_id.get(ref.id)
        body = desired_node(name, ref, node)
        if node is None:
            result.nodes[ref.id] = (CREATED, body)
        elif _node_matches(node, name):
            result.nodes[ref.id] = (UNCHANGED, body)
        else:
            result.nodes[ref.id] = (CHANGED, body)

        # The registry keeps one area per boundary id in a community, so a write
        # is refused while another area holds it (a renamed area, say).
        holders = [
            k for k, a in sorted(areas.items()) if k != area_key and _boundary_of(a) == ref.id
        ]

        current = areas.get(area_key)
        if current is None and len(holders) == 1 and holders[0] not in binding.boundaries:
            # A renamed area (D54): the registry holds this boundary under a key
            # the template no longer declares, and nothing holds the new key.
            # The registry moves it, members and all, in one request; what
            # still differs afterwards (its name) is the area's ordinary write.
            old_key = holders[0]
            stored = areas[old_key]
            result.renames[area_key] = old_key
            result.areas[area_key] = (RENAMED, desired_area(name, ref, stored), ref)
            if not _area_matches(stored, name, ref):
                result.rewrite_after_rename.add(area_key)
            result.held_by[area_key] = holders
            continue

        area_body = desired_area(name, ref, current)
        if current is None:
            outcome = CREATED
        elif _area_matches(current, name, ref):
            outcome = UNCHANGED
        else:
            outcome = CHANGED
        result.areas[area_key] = (outcome, area_body, ref)

        if holders and outcome != UNCHANGED:
            result.held_by[area_key] = holders

    for area_key, area in sorted(areas.items()):
        if area_key not in binding.boundaries:
            result.undeclared[area_key] = list(area.get("topology") or [])
    return result


# ---------------------------------------------------------------------------
# The set-up step (REQ-0014)
# ---------------------------------------------------------------------------


async def set_up_community(community: str) -> SetupStep:
    """``POST /reconcile/{community}`` with ``provisioning.reconcile``, reported, never raised."""
    from celine.onboarding.services import provisioning

    if not provisioning.provisioning_enabled():
        return SetupStep(
            status="skipped",
            reason="PROVISIONING_URL is not set, so the community's organization was not set up",
        )

    try:
        from celine.sdk.provisioning import ProvisioningClient

        from celine.onboarding.services.service_auth import celine_token_provider

        client = ProvisioningClient(
            base_url=provisioning.provisioning_url(),
            token_provider=celine_token_provider(RECONCILE_SCOPE),
        )
        result = await client.reconcile(community)
    except Exception as exc:  # every failure is reported; the areas are still written
        status = getattr(exc, "status_code", None)
        code = getattr(exc, "code", None)
        code = code if isinstance(code, str) else None
        logger.warning(
            "Registry sync: setting up community %s failed (%s %s %s)",
            community,
            type(exc).__name__,
            status,
            code,
        )
        if code == "community_not_found":
            reason = "the registry holds no such community"
        elif status in (401, 403):
            reason = (
                f"the provisioning service refused this service's token ({status}); "
                f"{settings.oidc_client_id!r} needs the optional scope {RECONCILE_SCOPE!r}"
            )
        elif status is not None:
            reason = f"the provisioning service answered {status}"
        else:
            reason = "the provisioning service could not be reached"
        return SetupStep(status="failed", code=code, reason=reason)

    return SetupStep(status="succeeded", members=result.members, created=result.created)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


async def _registry_read(client: RegistryAreas, community: str) -> dict[str, Any]:
    try:
        return await client.read_community(community)
    except RegistryRefusalError as exc:
        if exc.status_code == 404:
            raise SyncRefusedError(
                404,
                "community_not_found",
                f"The REC registry holds no community {community!r}; it has to exist "
                "before its areas can be synced",
            ) from None
        if exc.status_code in (401, 403):
            raise SyncRefusedError(
                502,
                "registry_refused",
                f"The REC registry refused this service's read ({exc.status_code}); "
                f"{settings.oidc_client_id!r} needs 'rec-registry.read'",
            ) from None
        raise SyncRefusedError(
            502, "registry_unavailable", f"The REC registry answered {exc.status_code}"
        ) from None
    except RegistryUnavailableError as exc:
        raise SyncRefusedError(
            502, "registry_unavailable", f"The REC registry did not answer ({exc})"
        ) from None


async def _count(client: RegistryAreas, community: str, area: str) -> int | None:
    try:
        return await client.count_members(community, area)
    except (RegistryRefusalError, RegistryUnavailableError) as exc:
        logger.warning(
            "Registry sync: members of an area of %s could not be counted (%s)",
            community,
            type(exc).__name__,
        )
        return None


async def read_plan(client: RegistryAreas, binding: RecRegistryBinding) -> Plan:
    """The registry community, read and compared: for a sync and for the drift check."""
    return plan(binding, await _registry_read(client, binding.community))


async def sync(rec_slug: str, *, dry_run: bool, prune: bool) -> SyncReport:
    """One sync of *rec_slug*'s template areas to its registry community.

    Raises :class:`SyncRefusedError` when the sync cannot run at all (nothing written
    anywhere). Otherwise answers the report, refusals included.
    """
    binding = await syncable_binding(rec_slug)
    community = binding.community
    client = registry()
    try:
        the_plan = await read_plan(client, binding)
        setup = SetupStep(status="not_run") if dry_run else await set_up_community(community)
        report = SyncReport(
            rec=rec_slug, community=community, dry_run=dry_run, prune=prune, setup=setup
        )
        run = _Run(client, community, the_plan, report, dry_run=dry_run, prune=prune)
        await run.execute()
    finally:
        await client.aclose()

    logger.info(
        "Registry sync of %s into %s (dry_run=%s prune=%s): %s",
        rec_slug,
        community,
        dry_run,
        prune,
        report.audit_detail(),
    )
    return report


class _Run:
    """One execution of a plan, or its dry run: the same decisions, no calls."""

    def __init__(
        self,
        client: RegistryAreas,
        community: str,
        the_plan: Plan,
        report: SyncReport,
        *,
        dry_run: bool,
        prune: bool,
    ) -> None:
        self.client = client
        self.community = community
        self.plan = the_plan
        self.report = report
        self.dry_run = dry_run
        self.prune = prune
        self.stopped = False
        self.deleted_areas: set[str] = set()
        self.failed_nodes: set[str] = set()
        #: Registry keys moved to a template key by a rename (D54).
        self.renamed_away: set[str] = set()
        #: The report items of the renamed areas, by their new key.
        self.renamed_items: dict[str, Item] = {}

    async def _write(self, item: Item, call) -> RegistryRefusalError | None:
        """Make one write, or record why it was not made. Answers the refusal, if any."""
        if self.stopped:
            item.outcome = NOT_RUN
            item.reason = "the registry stopped answering earlier in this run"
            return None
        if self.dry_run:
            return None
        try:
            await call()
        except RegistryRefusalError as exc:
            item.outcome = REFUSED
            item.code = exc.code
            item.reason = exc.detail
            return exc
        except RegistryUnavailableError as exc:
            item.outcome = NOT_RUN
            item.reason = f"the registry did not answer ({exc})"
            self.stopped = True
        return None

    async def execute(self) -> None:
        await self._nodes()
        await self._renames()
        await self._prune()
        await self._areas()
        await self._orphan_nodes()

    async def _nodes(self) -> None:
        for node_id, (outcome, body) in self.plan.nodes.items():
            item = Item(key=node_id, outcome=outcome, boundary_id=node_id)
            if outcome != UNCHANGED:
                await self._write(
                    item, lambda n=node_id, b=body: self.client.put_node(self.community, n, b)
                )
            if item.outcome in (REFUSED, NOT_RUN):
                self.failed_nodes.add(node_id)
            self.report.nodes.append(item)

    async def _renames(self) -> None:
        """Move each renamed area to its template key, members and all (D54).

        One registry request per area. The answer lists it once, under its new
        key, as ``renamed`` from the old one, with how many members moved; the
        old key is not listed as undeclared, and no ``prune`` is needed. A
        rename the registry refuses is reported ``refused`` with the registry's
        code, and leaves the old area where it was, reported as undeclared.
        """
        for new_key, old_key in self.plan.renames.items():
            _outcome, _body, ref = self.plan.areas[new_key]
            item = Item(key=new_key, outcome=RENAMED, boundary_id=ref.id, renamed_from=old_key)
            self.renamed_items[new_key] = item
            if ref.id in self.failed_nodes:
                item.outcome = REFUSED
                item.code = "topology_node_not_written"
                item.reason = "its primary-substation node could not be written"
                continue
            if self.dry_run:
                item.members = await _count(self.client, self.community, old_key)
                self.renamed_away.add(old_key)
                continue
            moved: dict[str, int | None] = {}

            async def _rename(o: str = old_key, n: str = new_key) -> None:
                moved["n"] = await self.client.rename_area(self.community, o, n)

            refusal = await self._write(item, _rename)
            if item.outcome == RENAMED:
                item.members = moved.get("n")
                self.renamed_away.add(old_key)
            elif refusal is not None:
                item.reason = f"the registry refused to move {old_key!r} here: {refusal.detail}"

    async def _prune(self) -> None:
        for area_key, _nodes in self.plan.undeclared.items():
            if area_key in self.renamed_away:
                continue
            item = Item(key=area_key, outcome=UNDECLARED)
            item.members = await _count(self.client, self.community, area_key)
            if self.prune:
                if item.members:
                    # The registry refuses it; say so without asking, in a dry
                    # run and in a real one alike.
                    item.outcome = REFUSED
                    item.code = "area_in_use"
                    item.reason = (
                        f"{item.members} member(s) are still in this area; move them "
                        "to another area first"
                    )
                else:
                    item.outcome = DELETED
                    await self._write(
                        item, lambda a=area_key: self.client.delete_area(self.community, a)
                    )
                    if item.outcome == REFUSED and item.code == "area_in_use":
                        # Members moved in since the count: count again.
                        item.members = await _count(self.client, self.community, area_key)
                        item.reason = (
                            f"{item.members if item.members is not None else 'Some'} "
                            "member(s) are still in this area; move them to another "
                            "area first"
                        )
                if item.outcome == DELETED:
                    self.deleted_areas.add(area_key)
            self.report.areas.append(item)

    async def _areas(self) -> None:
        """Write the areas, each once its boundary is free.

        The registry keeps one area per boundary id, so an area whose boundary
        another area holds waits: for a pruned area to go, or for a declared one
        to move to its own boundary earlier in the run. Whatever still waits when
        nothing moves any more is refused, naming the holder.
        """
        pending: list[str] = []
        items: dict[str, Item] = {}
        for area_key, (outcome, _body, ref) in self.plan.areas.items():
            if area_key in self.renamed_items:
                item = items[area_key] = self.renamed_items[area_key]
                if item.outcome == RENAMED and area_key in self.plan.rewrite_after_rename:
                    # Moved; now its ordinary write, which its own old key no
                    # longer blocks.
                    pending.append(area_key)
                continue
            item = Item(key=area_key, outcome=outcome, boundary_id=ref.id)
            items[area_key] = item
            if outcome == UNCHANGED:
                continue
            if ref.id in self.failed_nodes:
                item.outcome = REFUSED
                item.code = "topology_node_not_written"
                item.reason = "its primary-substation node could not be written"
                continue
            pending.append(area_key)

        moved: set[str] = set()
        progress = True
        while pending and progress:
            progress = False
            for area_key in list(pending):
                if self._holders(area_key, moved):
                    continue
                pending.remove(area_key)
                progress = True
                _outcome, body, _ref = self.plan.areas[area_key]
                item = items[area_key]
                await self._write(
                    item, lambda a=area_key, b=body: self.client.put_area(self.community, a, b)
                )
                if item.outcome in (CREATED, CHANGED, RENAMED):
                    moved.add(area_key)
                elif item.renamed_from and item.outcome == REFUSED:
                    item.reason = (
                        f"{item.reason} (the area was moved from {item.renamed_from!r} "
                        "first, with its members)"
                    )

        for area_key in pending:
            holders = self._holders(area_key, moved)
            item = items[area_key]
            item.outcome = REFUSED
            item.code = "boundary_held"
            named = ", ".join(repr(k) for k in holders)
            item.reason = f"the registry holds this boundary under area {named}"
            if all(k in self.plan.undeclared for k in holders):
                item.reason += (
                    ", which the template no longer declares: move its members, then "
                    "sync with prune"
                )

        self.report.areas.extend(items.values())

    def _holders(self, area_key: str, moved: set[str]) -> list[str]:
        """The areas still holding *area_key*'s boundary in the registry."""
        return [
            k
            for k in self.plan.held_by.get(area_key, [])
            if k not in self.deleted_areas and k not in moved and k not in self.renamed_away
        ]

    async def _orphan_nodes(self) -> None:
        """The nodes only a pruned area used, deleted after it (and never another's)."""
        wanted = set(self.plan.nodes)
        still_listed: set[str] = set()
        for key, nodes in self.plan.undeclared.items():
            if key not in self.deleted_areas:
                still_listed.update(nodes)
        for area_key in sorted(self.deleted_areas):
            for node_id in self.plan.undeclared.get(area_key, []):
                if node_id in wanted or node_id in still_listed:
                    continue
                if any(i.key == node_id for i in self.report.nodes):
                    continue
                item = Item(key=node_id, outcome=DELETED, boundary_id=node_id)
                refusal = await self._write(
                    item, lambda n=node_id: self.client.delete_node(self.community, n)
                )
                if refusal is not None and refusal.status_code == 404 and refusal.code is None:
                    # Not in the topology at all: nothing was left behind.
                    item.outcome = UNCHANGED
                    item.code = None
                    item.reason = "not in the registry's topology"
                self.report.nodes.append(item)


# ---------------------------------------------------------------------------
# Drift (REQ-0015)
# ---------------------------------------------------------------------------


async def drift(rec_slug: str) -> dict[str, Any]:
    """Whether the registry's areas and topology match the template, and where not.

    Reads only, with this service's default token (``rec-registry.read``).
    Checks no boundary against the Digital Twin and writes nothing.
    """
    manifest = template_service.load_manifest(rec_slug)
    block = manifest.get("rec_registry")
    if not template_service.declares_boundaries(block):
        return {
            "rec": rec_slug,
            "community": None,
            "status": "not_synced",
            "areas": [],
            "nodes": [],
        }
    try:
        binding = template_service.rec_registry_binding(rec_slug)
    except ValueError as exc:
        raise SyncRefusedError(422, "template_invalid", str(exc)) from None

    client = registry()
    try:
        the_plan = await read_plan(client, binding)
    finally:
        await client.aclose()

    areas = [
        {
            "key": key,
            "boundary_id": ref.id,
            "state": _state(outcome),
            "held_by": the_plan.held_by.get(key, []),
        }
        for key, (outcome, _body, ref) in the_plan.areas.items()
    ] + [
        {"key": key, "boundary_id": None, "state": "undeclared", "held_by": []}
        for key in the_plan.undeclared
    ]
    nodes = [
        {"key": node_id, "state": _state(outcome)}
        for node_id, (outcome, _body) in the_plan.nodes.items()
    ]
    matches = all(a["state"] == "matches" for a in areas) and all(
        n["state"] == "matches" for n in nodes
    )
    return {
        "rec": rec_slug,
        "community": binding.community,
        "status": "matches" if matches else "drift",
        "areas": areas,
        "nodes": nodes,
    }


def _state(outcome: str) -> str:
    # A renamed area is not in the registry under its key yet: the drift check
    # shows it missing, held by its old key.
    return {UNCHANGED: "matches", CREATED: "missing", CHANGED: "differs", RENAMED: "missing"}[
        outcome
    ]
