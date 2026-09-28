"""The registry sync: a template's areas pushed to the REC registry, by a realm admin.

`respx` stands in front of the registry, the Digital Twin and the provisioning
service (`fake_registry.py`, `fake_digital_twin.py`), which answer with their
real rules and codes. Requests leave through the real code paths — the route,
the service layer, the SDK's provisioning client — and every token is minted
and verified for real (`issue_token`). Synthetic data only.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from fake_digital_twin import boundary_manifest
from fake_registry import (
    COMMUNITY,
    PROVISIONING_URL,
    REGISTRY_URL,
    FakeProvisioning,
    FakeRegistry,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from celine.onboarding.api.admin import create_admin_router
from celine.onboarding.models.audit_log import AuditLog
from celine.onboarding.models.database import get_db
from celine.onboarding.security.middleware import AdminAuthMiddleware
from celine.onboarding.services import registry_sync

REC = "rec-b"
ORG = "example-rec"
SYNC = f"/api/admin/recs/{REC}/registry-sync"
DRIFT = f"/api/admin/recs/{REC}/registry-drift"

DEFAULT_TOKEN = "svc-onboarding:default"
WRITE_TOKEN = f"svc-onboarding:{registry_sync.COMMUNITY_WRITE_SCOPE}"
RECONCILE_TOKEN = f"svc-onboarding:{registry_sync.RECONCILE_SCOPE}"


class RecordingSession:
    """Enough `AsyncSession` to see the audit row; nothing is queried."""

    def __init__(self) -> None:
        self.added: list = []
        self.commits = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, *args, **kwargs):  # pragma: no cover - the failure path
        raise AssertionError("the registry sync must not query the database")

    scalar = scalars = get = execute

    @property
    def audit_rows(self) -> list[AuditLog]:
        return [row for row in self.added if isinstance(row, AuditLog)]


@pytest.fixture()
def services(fake_dt, monkeypatch):
    """The registry and the provisioning service beside the fake Digital Twin.

    This service's tokens are static strings naming the optional scope they were
    asked for, so a test can tell which call carried which.
    """
    from celine.sdk.auth import StaticTokenProvider

    from celine.onboarding.config.settings import settings
    from celine.onboarding.services import service_auth

    monkeypatch.setattr(settings, "rec_registry_url", REGISTRY_URL)
    monkeypatch.setattr(settings, "provisioning_url", PROVISIONING_URL)
    monkeypatch.setattr(
        service_auth,
        "celine_token_provider",
        lambda scope=None: StaticTokenProvider(f"svc-onboarding:{scope or 'default'}"),
    )
    registry = FakeRegistry(fake_dt.router)
    provisioning = FakeProvisioning(fake_dt.router)
    return registry, provisioning


@pytest.fixture()
def registry(services) -> FakeRegistry:
    return services[0]


@pytest.fixture()
def provisioning(services) -> FakeProvisioning:
    return services[1]


@pytest.fixture()
def template(seed_rec):
    """rec-b, two areas on two synthetic boundaries, filed into example-rec."""

    def _seed(**areas: str) -> dict:
        manifest = boundary_manifest(
            REC, **(areas or {"north": "AC000E00001", "south": "AC000E00002"})
        )
        manifest.pop("slug")
        return seed_rec(REC, organization=ORG, **manifest)

    _seed()
    return _seed


@pytest.fixture()
def db() -> RecordingSession:
    return RecordingSession()


@pytest.fixture()
def client(template, db) -> TestClient:
    app = FastAPI()
    app.add_middleware(AdminAuthMiddleware)
    app.include_router(create_admin_router())

    async def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


@pytest.fixture()
def realm_admin(issue_token) -> dict:
    token = issue_token(
        sub="platform-admin-sub",
        email="platform-admin@example.org",
        preferred_username="platform-admin",
        groups=["/admins"],
    )
    return {"Authorization": f"Bearer {token}"}


def _sync(client, headers, **params):
    query = "&".join(f"{k}={str(v).lower()}" for k, v in params.items())
    return client.post(f"{SYNC}?{query}" if query else SYNC, headers=headers)


def _outcomes(body: dict, kind: str) -> dict[str, str]:
    return {item["key"]: item["outcome"] for item in body[kind]}


# ---------------------------------------------------------------------------
# Who may sync (REQ-0009)
# ---------------------------------------------------------------------------


class TestOnlyARealmAdminMaySync:
    def test_a_realm_admin_may(self, client, registry, provisioning, realm_admin):
        """
        @verifies REQ-0009
        """
        assert _sync(client, realm_admin, dry_run=True).status_code == 200

    @pytest.mark.parametrize("group", ["admins", "managers"])
    def test_an_organization_group_may_not(
        self, client, registry, provisioning, operator_token, group
    ):
        """The REC's own admins administer its submissions, not its registry areas.

        @verifies REQ-0009
        """
        token = operator_token(ORG, group)
        response = _sync(client, {"Authorization": f"Bearer {token}"}, dry_run=True)
        assert response.status_code == 403
        assert registry.requests == [] and provisioning.calls == []

    @pytest.mark.parametrize("group", ["managers", "editors", "viewers"])
    def test_another_realm_group_may_not(self, client, registry, issue_token, group):
        """
        @verifies REQ-0009
        """
        token = issue_token(sub="op", email="op@example.org", groups=[f"/{group}"])
        response = _sync(client, {"Authorization": f"Bearer {token}"}, dry_run=True)
        assert response.status_code == 403
        assert registry.requests == []

    @pytest.mark.parametrize(
        "scopes", [("onboarding.admin",), ("onboarding.recs.read",), ("onboarding.*",)]
    )
    def test_no_service_account_may(self, client, registry, service_token, scopes):
        """`svc-onboarding-cli`'s client-credentials token included.

        @verifies REQ-0009
        """
        token = service_token(*scopes)
        response = _sync(client, {"Authorization": f"Bearer {token}"}, dry_run=True)
        assert response.status_code == 403
        assert registry.requests == []

    def test_no_token_is_401(self, client, registry):
        """
        @verifies REQ-0009
        """
        assert client.post(SYNC).status_code == 401

    def test_an_unknown_rec_is_404(self, client, registry, realm_admin):
        """
        @verifies REQ-0009
        """
        response = client.post("/api/admin/recs/nope/registry-sync", headers=realm_admin)
        assert response.status_code == 404

    def test_the_realm_admin_sees_the_capability(self, client, registry, realm_admin):
        """The console learns it may offer the sync from `/me`, as for every action.

        @verifies REQ-0009
        """
        [rec] = client.get("/api/admin/me", headers=realm_admin).json()["recs"]
        assert "recs.write" in rec["capabilities"]

    def test_an_org_admin_does_not(self, client, registry, operator_token):
        """
        @verifies REQ-0009
        """
        token = operator_token(ORG, "admins")
        [rec] = client.get("/api/admin/me", headers={"Authorization": f"Bearer {token}"}).json()[
            "recs"
        ]
        assert "recs.write" not in rec["capabilities"]

    def test_loading_a_template_writes_nothing(self, registry, provisioning, template):
        """Only the sync writes: a template (re)load never reaches the registry.

        @verifies REQ-0009
        """
        import asyncio

        from celine.onboarding.services import template_service

        template(north="AC000E00001")
        asyncio.run(template_service.ensure_fresh())
        assert registry.requests == [] and provisioning.calls == []


# ---------------------------------------------------------------------------
# Dry run (REQ-0010)
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_it_writes_nothing_and_lists_the_plan(
        self, client, registry, provisioning, realm_admin, db
    ):
        """
        @verifies REQ-0010
        """
        before = registry.snapshot()
        response = _sync(client, realm_admin, dry_run=True)

        assert response.status_code == 200
        body = response.json()
        assert body["dry_run"] is True
        assert _outcomes(body, "nodes") == {"AC000E00001": "created", "AC000E00002": "created"}
        assert _outcomes(body, "areas") == {"north": "created", "south": "created"}
        assert body["setup"]["status"] == "not_run"

        assert registry.writes() == []
        assert registry.snapshot() == before
        assert provisioning.calls == []
        # Nothing to record: a dry run changed nothing.
        assert db.audit_rows == []

    def test_it_lists_the_undeclared_areas_and_their_member_counts(
        self, client, registry, provisioning, realm_admin
    ):
        """
        @verifies REQ-0010
        """
        registry.seed_area("north", "AC000E00001")
        registry.seed_area("south", "AC000E00002")
        registry.seed_area("east", "AC000E00003")
        registry.add_member("east", 3)

        body = _sync(client, realm_admin, dry_run=True, prune=True).json()

        [east] = [a for a in body["areas"] if a["key"] == "east"]
        assert east["outcome"] == "refused"
        assert east["code"] == "area_in_use"
        assert east["members"] == 3
        assert registry.writes() == []

    def test_a_dry_run_with_prune_says_what_would_go(
        self, client, registry, provisioning, realm_admin
    ):
        """
        @verifies REQ-0010
        """
        registry.seed_area("north", "AC000E00001")
        registry.seed_area("south", "AC000E00002")
        registry.seed_area("east", "AC000E00003")

        body = _sync(client, realm_admin, dry_run=True, prune=True).json()

        assert _outcomes(body, "areas")["east"] == "deleted"
        assert _outcomes(body, "nodes")["AC000E00003"] == "deleted"
        assert registry.writes() == []
        assert "AC000E00003" in {n["id"] for n in registry.snapshot()["topology"]}


# ---------------------------------------------------------------------------
# What is written, and a second run (REQ-0011)
# ---------------------------------------------------------------------------


class TestTheFirstSync:
    def test_it_creates_each_node_then_each_area(self, client, registry, provisioning, realm_admin):
        """
        @verifies REQ-0011
        """
        response = _sync(client, realm_admin)

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert _outcomes(body, "nodes") == {"AC000E00001": "created", "AC000E00002": "created"}
        assert _outcomes(body, "areas") == {"north": "created", "south": "created"}

        community = registry.snapshot()
        assert community["areas"] == {
            "north": {
                "name": "north",
                "boundary": {"source": "gse_cabine_primarie", "id": "AC000E00001"},
                "topology": ["AC000E00001"],
            },
            "south": {
                "name": "south",
                "boundary": {"source": "gse_cabine_primarie", "id": "AC000E00002"},
                "topology": ["AC000E00002"],
            },
        }
        assert sorted(community["topology"], key=lambda n: n["id"]) == [
            {"id": "AC000E00001", "type": "primary_substation", "name": "north"},
            {"id": "AC000E00002", "type": "primary_substation", "name": "south"},
        ]

        # Each area's node is written before the area that needs it.
        paths = [path for _method, path, _token in registry.writes()]
        base = f"/admin/communities/{COMMUNITY}"
        assert paths.index(f"{base}/topology/AC000E00001") < paths.index(f"{base}/areas/north")
        assert paths.index(f"{base}/topology/AC000E00002") < paths.index(f"{base}/areas/south")

    def test_writes_ask_for_the_write_scope_and_reads_do_not(
        self, client, registry, provisioning, realm_admin
    ):
        """`rec-registry.community.write` is requested for the writes only (D37).

        @verifies REQ-0009
        """
        _sync(client, realm_admin)

        assert {token for _m, _p, token in registry.writes()} == {WRITE_TOKEN}
        assert {token for _m, _p, token in registry.reads()} == {DEFAULT_TOKEN}

    def test_the_audit_row_counts_and_names_nobody(
        self, client, registry, provisioning, realm_admin, db
    ):
        """
        @verifies REQ-0011
        """
        registry.seed_area("east", "AC000E00003")
        registry.add_member("east", 2)
        _sync(client, realm_admin)

        [row] = db.audit_rows
        assert row.action == "registry_sync"
        assert row.rec_slug == REC
        assert row.actor_email == "platform-admin@example.org"
        assert "areas_created=2" in row.detail and "areas_undeclared=1" in row.detail
        assert "Example Person" not in row.detail


class TestASecondSync:
    def test_it_changes_nothing(self, client, registry, provisioning, realm_admin):
        """
        @verifies REQ-0011
        """
        assert _sync(client, realm_admin).status_code == 200
        registry.requests.clear()
        before = registry.snapshot()

        body = _sync(client, realm_admin).json()

        assert set(_outcomes(body, "nodes").values()) == {"unchanged"}
        assert set(_outcomes(body, "areas").values()) == {"unchanged"}
        assert registry.writes() == []
        assert registry.snapshot() == before

    def test_it_keeps_what_the_template_does_not_own(
        self, client, registry, provisioning, realm_admin
    ):
        """A node's operator and parent, an area's location, survive a changed write.

        @verifies REQ-0011
        """
        community = registry.communities[COMMUNITY]
        community["topology"] = [
            {"id": "AC000E00001", "type": "substation", "name": "x", "operator_id": "dso-1",
             "parent": "AC000E00009"},
        ]  # fmt: skip
        community["areas"]["north"] = {
            "name": "North",
            "boundary": {"source": "gse_cabine_primarie", "id": "AC000E00001"},
            "topology": ["AC000E00001"],
            "location": {"lat": 0.05, "lon": 0.05},
        }

        body = _sync(client, realm_admin).json()

        assert _outcomes(body, "nodes")["AC000E00001"] == "changed"
        assert _outcomes(body, "areas")["north"] == "changed"
        [node] = [n for n in registry.snapshot()["topology"] if n["id"] == "AC000E00001"]
        assert node == {
            "id": "AC000E00001",
            "type": "primary_substation",
            "name": "north",
            "operator_id": "dso-1",
            "parent": "AC000E00009",
        }
        assert registry.snapshot()["areas"]["north"]["location"] == {"lat": 0.05, "lon": 0.05}


# ---------------------------------------------------------------------------
# Additive by default; prune (REQ-0011, REQ-0012)
# ---------------------------------------------------------------------------


class TestRemoval:
    def test_an_undeclared_area_is_reported_and_kept(
        self, client, registry, provisioning, realm_admin
    ):
        """
        @verifies REQ-0011
        """
        registry.seed_area("east", "AC000E00003")
        registry.add_member("east", 1)

        body = _sync(client, realm_admin).json()

        [east] = [a for a in body["areas"] if a["key"] == "east"]
        assert east["outcome"] == "undeclared" and east["members"] == 1
        assert "east" in registry.snapshot()["areas"]
        assert not any(m == "DELETE" for m, _p, _t in registry.requests)

    def test_prune_deletes_it_and_then_its_orphan_node(
        self, client, registry, provisioning, realm_admin
    ):
        """
        @verifies REQ-0012
        """
        registry.seed_area("east", "AC000E00003")

        body = _sync(client, realm_admin, prune=True).json()

        assert _outcomes(body, "areas")["east"] == "deleted"
        assert _outcomes(body, "nodes")["AC000E00003"] == "deleted"
        community = registry.snapshot()
        assert "east" not in community["areas"]
        assert "AC000E00003" not in {n["id"] for n in community["topology"]}
        deletes = [p for m, p, _t in registry.requests if m == "DELETE"]
        assert deletes == [
            f"/admin/communities/{COMMUNITY}/areas/east",
            f"/admin/communities/{COMMUNITY}/topology/AC000E00003",
        ]

    def test_prune_on_an_area_with_members_answers_the_count(
        self, client, registry, provisioning, realm_admin
    ):
        """The admin moves them first; nothing of that area is deleted.

        @verifies REQ-0012
        """
        registry.seed_area("east", "AC000E00003")
        registry.add_member("east", 4)
        registry.add_member("north", 2)

        response = _sync(client, realm_admin, prune=True)

        assert response.status_code == 200
        body = response.json()
        [east] = [a for a in body["areas"] if a["key"] == "east"]
        assert east == {
            "key": "east",
            "boundary_id": None,
            "outcome": "refused",
            "code": "area_in_use",
            "reason": "4 member(s) are still in this area; move them to another area first",
            "members": 4,
            "renamed_from": None,
        }
        assert body["ok"] is False
        assert "east" in registry.snapshot()["areas"]
        assert "AC000E00003" in {n["id"] for n in registry.snapshot()["topology"]}
        # The members were counted, never returned.
        assert "Example Person" not in response.text

    def test_a_member_moved_in_after_the_count_is_the_registrys_refusal(
        self, client, registry, provisioning, realm_admin, monkeypatch
    ):
        """
        @verifies REQ-0012
        """
        registry.seed_area("east", "AC000E00003")
        calls = {"n": 0}
        original = registry_sync.RegistryAreas.count_members

        async def _racy(self, community, area):
            calls["n"] += 1
            if calls["n"] == 1:
                return 0
            return await original(self, community, area)

        monkeypatch.setattr(registry_sync.RegistryAreas, "count_members", _racy)
        registry.add_member("east", 1)

        body = _sync(client, realm_admin, prune=True).json()

        [east] = [a for a in body["areas"] if a["key"] == "east"]
        assert east["outcome"] == "refused" and east["code"] == "area_in_use"
        assert east["members"] == 1

    def test_two_areas_swapping_boundaries_across_runs(
        self, client, registry, provisioning, realm_admin, template
    ):
        """An area moving off a boundary frees it for another in the same run.

        @verifies REQ-0011
        """
        registry.seed_area("north", "AC000E00001")
        template(north="AC000E00003", south="AC000E00001")

        body = _sync(client, realm_admin).json()

        assert _outcomes(body, "areas") == {"north": "changed", "south": "created"}
        assert registry.snapshot()["areas"]["south"]["boundary"]["id"] == "AC000E00001"


# ---------------------------------------------------------------------------
# A renamed area moves with its members (REQ-0011, D54)
# ---------------------------------------------------------------------------


class TestARenamedArea:
    """The template renames an area: same boundary, new key.

    The registry keeps one area per boundary, so the new key cannot be created
    beside the old one, and the old one cannot be deleted while members hold
    it. The sync asks the registry to move it instead, members and all.
    """

    def test_it_is_renamed_and_its_members_follow(
        self, client, registry, provisioning, realm_admin
    ):
        """A renamed area is created under its new key and the old one is reported:
        as the item's `renamed_from`, and no longer in the registry.

        @verifies REQ-0011
        """
        registry.seed_area("old-north", "AC000E00001")
        registry.seed_area("south", "AC000E00002")
        registry.add_member("old-north", 3)
        registry.add_member("south", 1)

        response = _sync(client, realm_admin)

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert _outcomes(body, "areas") == {"north": "renamed", "south": "unchanged"}
        [north] = [a for a in body["areas"] if a["key"] == "north"]
        assert north["renamed_from"] == "old-north"
        assert north["members"] == 3
        assert north["boundary_id"] == "AC000E00001"
        assert body["summary"]["areas"] == {"renamed": 1, "unchanged": 1}

        community = registry.snapshot()
        assert set(community["areas"]) == {"north", "south"}
        assert community["areas"]["north"] == {
            "name": "north",
            "boundary": {"source": "gse_cabine_primarie", "id": "AC000E00001"},
            "topology": ["AC000E00001"],
        }
        # The members moved with it; nobody else did.
        areas_of = sorted(m["area"] for m in registry.members[COMMUNITY])
        assert areas_of == ["north", "north", "north", "south"]
        # The node keeps its id and takes the new name.
        [node] = [n for n in community["topology"] if n["id"] == "AC000E00001"]
        assert node["name"] == "north"

    def test_it_is_one_rename_request_then_the_areas_write(
        self, client, registry, provisioning, realm_admin
    ):
        """No prune, no delete: the rename, then the area's ordinary write for its name.

        @verifies REQ-0011
        """
        registry.seed_area("old-north", "AC000E00001")
        registry.seed_area("south", "AC000E00002")
        registry.add_member("old-north", 1)

        _sync(client, realm_admin)

        base = f"/admin/communities/{COMMUNITY}"
        writes = [(m, p) for m, p, _t in registry.writes()]
        assert ("POST", f"{base}/areas/old-north/rename") in writes
        assert not any(m == "DELETE" for m, _p in writes)
        rename = writes.index(("POST", f"{base}/areas/old-north/rename"))
        assert writes.index(("PUT", f"{base}/topology/AC000E00001")) < rename
        assert rename < writes.index(("PUT", f"{base}/areas/north"))
        assert {t for m, _p, t in registry.writes() if m == "POST"} == {WRITE_TOKEN}

    def test_an_empty_area_is_renamed_too(self, client, registry, provisioning, realm_admin):
        """
        @verifies REQ-0011
        """
        registry.seed_area("old-north", "AC000E00001")

        body = _sync(client, realm_admin).json()

        [north] = [a for a in body["areas"] if a["key"] == "north"]
        assert north["outcome"] == "renamed" and north["members"] == 0
        assert "old-north" not in _outcomes(body, "areas")
        assert set(registry.snapshot()["areas"]) == {"north", "south"}

    def test_with_prune_it_is_renamed_not_deleted(
        self, client, registry, provisioning, realm_admin
    ):
        """
        @verifies REQ-0011
        @verifies REQ-0012
        """
        registry.seed_area("old-north", "AC000E00001")
        registry.seed_area("south", "AC000E00002")
        registry.add_member("old-north", 2)

        body = _sync(client, realm_admin, prune=True).json()

        assert _outcomes(body, "areas") == {"north": "renamed", "south": "unchanged"}
        assert body["ok"] is True
        community = registry.snapshot()
        assert set(community["areas"]) == {"north", "south"}
        # The node is the renamed area's: not an orphan, not deleted.
        assert "AC000E00001" in {n["id"] for n in community["topology"]}
        assert not any(m == "DELETE" for m, _p, _t in registry.requests)

    def test_a_dry_run_lists_it_and_writes_nothing(
        self, client, registry, provisioning, realm_admin
    ):
        """
        @verifies REQ-0010
        @verifies REQ-0011
        """
        registry.seed_area("old-north", "AC000E00001")
        registry.add_member("old-north", 2)
        before = registry.snapshot()

        body = _sync(client, realm_admin, dry_run=True).json()

        [north] = [a for a in body["areas"] if a["key"] == "north"]
        assert north["outcome"] == "renamed"
        assert north["renamed_from"] == "old-north"
        assert north["members"] == 2
        assert "old-north" not in _outcomes(body, "areas")
        assert registry.writes() == []
        assert registry.snapshot() == before
        assert sorted(m["area"] for m in registry.members[COMMUNITY]) == ["old-north"] * 2

    def test_a_second_run_changes_nothing(self, client, registry, provisioning, realm_admin):
        """
        @verifies REQ-0011
        """
        registry.seed_area("old-north", "AC000E00001")
        registry.add_member("old-north", 1)
        _sync(client, realm_admin)
        registry.requests.clear()

        body = _sync(client, realm_admin).json()

        assert set(_outcomes(body, "areas").values()) == {"unchanged"}
        assert registry.writes() == []

    def test_the_old_area_keeps_what_the_template_does_not_own(
        self, client, registry, provisioning, realm_admin
    ):
        """
        @verifies REQ-0011
        """
        registry.seed_area("old-north", "AC000E00001")
        registry.communities[COMMUNITY]["areas"]["old-north"]["location"] = {
            "lat": 0.05,
            "lon": 0.05,
        }

        _sync(client, realm_admin)

        north = registry.snapshot()["areas"]["north"]
        assert north["location"] == {"lat": 0.05, "lon": 0.05}
        assert north["name"] == "north"

    def test_a_key_the_registry_already_holds_is_not_renamed_onto(
        self, client, registry, provisioning, realm_admin, template
    ):
        """The new key exists on another boundary: no rename, the boundary is held.

        @verifies REQ-0011
        """
        template(north="AC000E00001")
        registry.seed_area("old-north", "AC000E00001")
        registry.seed_area("north", "AC000E00003")
        registry.add_member("old-north", 1)

        body = _sync(client, realm_admin).json()

        [north] = [a for a in body["areas"] if a["key"] == "north"]
        assert north["outcome"] == "refused" and north["code"] == "boundary_held"
        assert "'old-north'" in north["reason"] and "prune" in north["reason"]
        assert not any(m == "POST" for m, _p, _t in registry.requests)
        assert "old-north" in registry.snapshot()["areas"]

    def test_a_refused_rename_is_reported_and_the_old_area_stays(
        self, client, registry, provisioning, realm_admin
    ):
        """
        @verifies REQ-0011
        """
        registry.seed_area("old-north", "AC000E00001")
        registry.add_member("old-north", 2)
        registry._rename = lambda request, c, area: (
            registry._record(request),
            httpx.Response(409, json={"detail": "taken", "code": "area_key_taken"}),
        )[1]

        body = _sync(client, realm_admin).json()

        [north] = [a for a in body["areas"] if a["key"] == "north"]
        assert north["outcome"] == "refused" and north["code"] == "area_key_taken"
        assert "'old-north'" in north["reason"]
        [old] = [a for a in body["areas"] if a["key"] == "old-north"]
        assert old["outcome"] == "undeclared" and old["members"] == 2
        assert body["ok"] is False
        assert "north" not in registry.snapshot()["areas"]

    def test_the_audit_row_counts_the_rename(self, client, registry, provisioning, realm_admin, db):
        """
        @verifies REQ-0011
        """
        registry.seed_area("old-north", "AC000E00001")
        registry.add_member("old-north", 2)

        _sync(client, realm_admin)

        [row] = db.audit_rows
        assert "areas_renamed=1" in row.detail
        assert "Example Person" not in row.detail and "m-0000" not in row.detail

    def test_the_drift_check_shows_it_before_the_sync(
        self, client, registry, provisioning, realm_admin
    ):
        """
        @verifies REQ-0015
        """
        registry.seed_area("old-north", "AC000E00001")
        registry.seed_area("south", "AC000E00002")

        body = client.get(DRIFT, headers=realm_admin).json()

        states = {a["key"]: (a["state"], a["held_by"]) for a in body["areas"]}
        assert states["north"] == ("missing", ["old-north"])
        assert states["old-north"] == ("undeclared", [])


# ---------------------------------------------------------------------------
# An area's display name (REQ-0021, D57)
# ---------------------------------------------------------------------------


def _named_template(seed_rec, **names: str | None) -> None:
    manifest = boundary_manifest(REC, north="AC000E00001", south="AC000E00002")
    for key, name in names.items():
        if name is not None:
            manifest["rec_registry"]["areas"][key]["name"] = name
    manifest.pop("slug")
    seed_rec(REC, organization=ORG, **manifest)


class TestAnAreasDisplayName:
    def test_it_is_written_to_the_area_and_its_node(
        self, client, registry, provisioning, realm_admin, seed_rec
    ):
        """
        @verifies REQ-0021
        """
        _named_template(seed_rec, north="North valley")

        body = _sync(client, realm_admin).json()

        assert body["ok"] is True
        community = registry.snapshot()
        assert community["areas"]["north"]["name"] == "North valley"
        assert community["areas"]["south"]["name"] == "south"
        names = {n["id"]: n["name"] for n in community["topology"]}
        assert names == {"AC000E00001": "North valley", "AC000E00002": "south"}

    def test_a_second_run_changes_nothing(
        self, client, registry, provisioning, realm_admin, seed_rec
    ):
        """
        @verifies REQ-0021
        """
        _named_template(seed_rec, north="North valley")
        _sync(client, realm_admin)
        registry.requests.clear()

        body = _sync(client, realm_admin).json()

        assert set(_outcomes(body, "areas").values()) == {"unchanged"}
        assert set(_outcomes(body, "nodes").values()) == {"unchanged"}
        assert registry.writes() == []

    def test_a_changed_name_changes_the_area_and_the_node(
        self, client, registry, provisioning, realm_admin, seed_rec
    ):
        """
        @verifies REQ-0021
        """
        _sync(client, realm_admin)
        _named_template(seed_rec, north="North valley")

        body = _sync(client, realm_admin).json()

        assert _outcomes(body, "areas") == {"north": "changed", "south": "unchanged"}
        assert _outcomes(body, "nodes") == {"AC000E00001": "changed", "AC000E00002": "unchanged"}
        assert registry.snapshot()["areas"]["north"]["name"] == "North valley"

    def test_a_renamed_area_takes_the_name(
        self, client, registry, provisioning, realm_admin, seed_rec
    ):
        """
        @verifies REQ-0021
        """
        registry.seed_area("old-north", "AC000E00001")
        registry.add_member("old-north", 1)
        _named_template(seed_rec, north="North valley")

        body = _sync(client, realm_admin).json()

        assert _outcomes(body, "areas")["north"] == "renamed"
        assert registry.snapshot()["areas"]["north"]["name"] == "North valley"

    def test_the_drift_check_compares_the_name(
        self, client, registry, provisioning, realm_admin, seed_rec
    ):
        """
        @verifies REQ-0015
        @verifies REQ-0021
        """
        _sync(client, realm_admin)
        _named_template(seed_rec, north="North valley")

        body = client.get(DRIFT, headers=realm_admin).json()

        assert {a["key"]: a["state"] for a in body["areas"]} == {
            "north": "differs",
            "south": "matches",
        }

    @pytest.mark.parametrize(
        "name",
        ["", "  ", " North", "x" * 129, 7, ["North"]],
        ids=["empty", "blank", "padded", "too-long", "number", "list"],
    )
    def test_a_bad_name_refuses_the_template(self, client, registry, realm_admin, seed_rec, name):
        """
        @verifies REQ-0013
        @verifies REQ-0021
        """
        _named_template(seed_rec, north=name)

        response = _sync(client, realm_admin)

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "template_invalid"
        assert "name" in response.json()["detail"]["message"]
        assert registry.requests == []


# ---------------------------------------------------------------------------
# Validation before anything is written (REQ-0013)
# ---------------------------------------------------------------------------


class TestValidation:
    def test_an_unknown_boundary_refuses_the_template(
        self, client, registry, provisioning, realm_admin, template
    ):
        """
        @verifies REQ-0013
        """
        template(north="AC000E00001", south="AC000E00009")

        response = _sync(client, realm_admin)

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "template_invalid"
        assert "AC000E00009" in response.json()["detail"]["message"]
        assert registry.requests == [] and provisioning.calls == []

    def test_two_areas_on_one_boundary_refuse_the_template(
        self, client, registry, provisioning, realm_admin, template
    ):
        """
        @verifies REQ-0013
        """
        template(north="AC000E00001", south="AC000E00001")

        response = _sync(client, realm_admin, dry_run=True)

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "template_invalid"
        assert registry.requests == [] and provisioning.calls == []

    def test_an_invalid_area_key_refuses_the_template(
        self, client, registry, realm_admin, seed_rec
    ):
        """
        @verifies REQ-0013
        """
        manifest = boundary_manifest(REC, north="AC000E00001")
        manifest["rec_registry"]["areas"] = {
            "no/slash": {"boundary": {"source": "gse_cabine_primarie", "id": "AC000E00001"}}
        }
        manifest.pop("slug")
        seed_rec(REC, organization=ORG, **manifest)

        response = _sync(client, realm_admin)
        assert response.status_code == 422
        assert registry.requests == []

    @pytest.mark.parametrize(
        "change",
        [
            {"coverage": {"rules": [{"type": "municipality", "values": ["Springfield"]}]}},
            {"steps": ["consents", "personal", "review"]},
            {"steps": ["eligibility", "consents", "personal", "review"]},
        ],
        ids=["coverage-rules", "no-eligibility-step", "eligibility-before-consents"],
    )
    def test_the_import_checks_apply(
        self, client, registry, realm_admin, template, seed_rec, change
    ):
        """
        @verifies REQ-0013
        """
        manifest = template()
        manifest.update(change)
        response = _sync(client, realm_admin)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "template_invalid"
        assert registry.requests == []

    def test_a_municipality_template_is_not_synced(self, client, registry, realm_admin, seed_rec):
        """
        @verifies REQ-0013
        """
        seed_rec(
            REC,
            organization=ORG,
            rec_registry={"community": COMMUNITY, "default_area": "north", "areas": {}},
        )
        response = _sync(client, realm_admin)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "template_not_syncable"
        assert registry.requests == []

    def test_an_unreachable_digital_twin_stops_the_sync(
        self, client, registry, provisioning, realm_admin, fake_dt
    ):
        """
        @verifies REQ-0013
        """
        fake_dt.failure = httpx.ConnectError("refused")
        response = _sync(client, realm_admin)
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "boundaries_unavailable"
        assert registry.requests == [] and provisioning.calls == []

    def test_a_community_the_registry_does_not_hold_stops_the_sync(
        self, client, registry, provisioning, realm_admin
    ):
        """
        @verifies REQ-0014
        """
        del registry.communities[COMMUNITY]
        response = _sync(client, realm_admin)
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "community_not_found"
        assert registry.writes() == [] and provisioning.calls == []

    def test_an_unconfigured_registry_is_503(self, client, registry, realm_admin, monkeypatch):
        """
        @verifies REQ-0013
        """
        from celine.onboarding.config.settings import settings

        monkeypatch.setattr(settings, "rec_registry_url", "")
        response = _sync(client, realm_admin)
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "registry_not_configured"

    def test_an_unreachable_registry_is_502_and_writes_nothing(
        self, client, registry, provisioning, realm_admin
    ):
        """
        @verifies REQ-0013
        """
        registry.failure = httpx.ConnectError("refused")
        response = _sync(client, realm_admin)
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "registry_unavailable"
        assert provisioning.calls == []


# ---------------------------------------------------------------------------
# Setting the community up (REQ-0014)
# ---------------------------------------------------------------------------


class TestSetUp:
    def test_a_real_sync_reconciles_first_with_the_reconcile_scope(
        self, client, registry, provisioning, realm_admin
    ):
        """
        @verifies REQ-0014
        """
        order: list[str] = []
        registry_record = registry._record
        provisioning_answer = provisioning._reconcile

        def _registry(request):
            if request.method in ("PUT", "DELETE"):
                order.append("registry-write")
            return registry_record(request)

        def _reconcile(request, c):
            order.append("reconcile")
            return provisioning_answer(request, c)

        registry._record = _registry
        provisioning._reconcile = _reconcile

        body = _sync(client, realm_admin).json()

        assert provisioning.calls == [(COMMUNITY, RECONCILE_TOKEN)]
        assert order[0] == "reconcile" and "registry-write" in order
        assert body["setup"] == {
            "status": "succeeded",
            "code": None,
            "reason": None,
            "members": 0,
            "created": 0,
        }

    @pytest.mark.parametrize(
        "failure,code",
        [
            (500, "provisioning_failed"),
            (404, "community_not_found"),
            (403, "insufficient_scope"),
            (httpx.ConnectError("refused"), None),
        ],
        ids=["error", "no-community", "no-scope", "unreachable"],
    )
    def test_a_failed_reconcile_is_reported_and_the_areas_are_still_written(
        self, client, registry, provisioning, realm_admin, failure, code
    ):
        """
        @verifies REQ-0014
        """
        provisioning.failure = failure
        if code:
            provisioning.code = code

        response = _sync(client, realm_admin)

        assert response.status_code == 200
        body = response.json()
        assert body["setup"]["status"] == "failed"
        assert body["setup"]["reason"]
        assert body["setup"]["code"] == code
        assert body["ok"] is True
        assert set(registry.snapshot()["areas"]) == {"north", "south"}

    def test_no_provisioning_service_skips_the_step(
        self, client, registry, provisioning, realm_admin, monkeypatch
    ):
        """
        @verifies REQ-0014
        """
        from celine.onboarding.config.settings import settings

        monkeypatch.setattr(settings, "provisioning_url", "")

        body = _sync(client, realm_admin).json()

        assert body["setup"]["status"] == "skipped"
        assert "PROVISIONING_URL" in body["setup"]["reason"]
        assert provisioning.calls == []
        assert set(registry.snapshot()["areas"]) == {"north", "south"}

    def test_a_rerun_completes_the_set_up_and_changes_no_area(
        self, client, registry, provisioning, realm_admin
    ):
        """
        @verifies REQ-0014
        """
        provisioning.failure = httpx.ConnectError("refused")
        _sync(client, realm_admin)
        provisioning.failure = None
        registry.requests.clear()

        body = _sync(client, realm_admin).json()

        assert body["setup"]["status"] == "succeeded"
        assert registry.writes() == []
        assert set(_outcomes(body, "areas").values()) == {"unchanged"}

    def test_the_failure_is_audited_with_the_step(
        self, client, registry, provisioning, realm_admin, db
    ):
        """
        @verifies REQ-0014
        """
        provisioning.failure = 500
        _sync(client, realm_admin)
        [row] = db.audit_rows
        assert "setup=failed" in row.detail


# ---------------------------------------------------------------------------
# Refusals from the registry during a run
# ---------------------------------------------------------------------------


class TestRegistryRefusals:
    def test_a_refused_node_refuses_its_area_and_nothing_else(
        self, client, registry, provisioning, realm_admin
    ):
        """
        @verifies REQ-0011
        """
        original = registry._node

        def _refuse_one(request, c, node):
            if node == "AC000E00002":
                registry._record(request)
                return httpx.Response(422, json={"detail": "no", "code": "invalid_area_boundary"})
            return original(request, c, node)

        registry._node = _refuse_one

        body = _sync(client, realm_admin).json()

        assert _outcomes(body, "nodes") == {"AC000E00001": "created", "AC000E00002": "refused"}
        assert _outcomes(body, "areas") == {"north": "created", "south": "refused"}
        [south] = [a for a in body["areas"] if a["key"] == "south"]
        assert south["code"] == "topology_node_not_written"
        assert body["ok"] is False

    def test_a_registry_that_stops_answering_leaves_the_rest_not_run(
        self, client, registry, provisioning, realm_admin
    ):
        """
        @verifies REQ-0011
        """
        original = registry._record

        def _die_on_write(request):
            if request.method == "PUT":
                raise httpx.ConnectError("gone")
            return original(request)

        registry._record = _die_on_write

        body = _sync(client, realm_admin).json()

        assert set(_outcomes(body, "nodes").values()) == {"not_run"}
        assert set(_outcomes(body, "areas").values()) == {"refused"}
        assert body["ok"] is False

    def test_a_write_answered_with_nothing_readable_stops_the_run(
        self, client, registry, provisioning, realm_admin
    ):
        """A `200` the SDK cannot read is a registry that stopped answering.

        @verifies REQ-0011
        """

        def _garbled(request, c, node):
            registry._record(request)
            return httpx.Response(200, text="<html>proxy</html>")

        registry._node = _garbled

        body = _sync(client, realm_admin).json()

        assert set(_outcomes(body, "nodes").values()) == {"not_run"}
        assert body["ok"] is False

    async def test_every_path_segment_is_quoted_on_its_own(self, fake_dt, registry):
        """No key can reach another route: each segment is quoted with `safe=''`."""
        seen: list[bytes] = []

        def _answer(request, **_kw):
            seen.append(request.url.raw_path)
            if request.url.path.endswith("/members"):
                return httpx.Response(200, json={"items": [], "next_cursor": None})
            if request.url.path.endswith("/rename"):
                detail = json.loads(registry._detail(COMMUNITY).content)
                return httpx.Response(
                    200,
                    json={"old_key": "a", "new_key": "b", "members_moved": 0, "community": detail},
                )
            return registry._detail(COMMUNITY)

        fake_dt.router.route(url__startswith="http://quoted.test/").mock(side_effect=_answer)
        areas = registry_sync.RegistryAreas("http://quoted.test")
        odd = "a/b c?d"
        node = {"id": odd, "type": "primary_substation", "name": "n"}
        area = {"name": "n", "boundary": {"source": "s", "id": odd}, "topology": [odd]}
        try:
            await areas.read_community(odd)
            await areas.count_members(odd, odd)
            await areas.put_node(odd, odd, node)
            await areas.delete_node(odd, odd)
            await areas.put_area(odd, odd, area)
            await areas.delete_area(odd, odd)
            await areas.rename_area(odd, odd, "b")
        finally:
            await areas.aclose()

        quoted = "a%2Fb%20c%3Fd"
        paths = [p.decode().split("?")[0] for p in seen]
        assert paths == [
            f"/admin/communities/{quoted}",
            f"/admin/communities/{quoted}/members",
            f"/admin/communities/{quoted}/topology/{quoted}",
            f"/admin/communities/{quoted}/topology/{quoted}",
            f"/admin/communities/{quoted}/areas/{quoted}",
            f"/admin/communities/{quoted}/areas/{quoted}",
            f"/admin/communities/{quoted}/areas/{quoted}/rename",
        ]


# ---------------------------------------------------------------------------
# Drift (REQ-0015)
# ---------------------------------------------------------------------------


class TestDrift:
    def test_after_a_sync_the_registry_matches(self, client, registry, provisioning, realm_admin):
        """
        @verifies REQ-0015
        """
        _sync(client, realm_admin)
        registry.requests.clear()

        body = client.get(DRIFT, headers=realm_admin).json()

        assert body["status"] == "matches"
        assert {a["key"]: a["state"] for a in body["areas"]} == {
            "north": "matches",
            "south": "matches",
        }
        # A read, with this service's default token, and nothing else.
        assert registry.writes() == []
        assert {token for _m, _p, token in registry.reads()} == {DEFAULT_TOKEN}

    def test_an_area_reintroduced_by_a_bundle_import_shows(
        self, client, registry, provisioning, realm_admin
    ):
        """
        @verifies REQ-0015
        """
        _sync(client, realm_admin)
        registry.seed_area("old-east", "AC000E00003")
        registry.communities[COMMUNITY]["areas"]["south"]["name"] = "South (imported)"

        body = client.get(DRIFT, headers=realm_admin).json()

        assert body["status"] == "drift"
        states = {a["key"]: a["state"] for a in body["areas"]}
        assert states == {"north": "matches", "south": "differs", "old-east": "undeclared"}

    def test_a_missing_area_shows(self, client, registry, realm_admin, fake_dt):
        """
        @verifies REQ-0015
        """
        body = client.get(DRIFT, headers=realm_admin).json()
        assert body["status"] == "drift"
        assert {a["state"] for a in body["areas"]} == {"missing"}
        # The drift check asks the Digital Twin nothing.
        assert fake_dt.calls() == []

    @pytest.mark.parametrize("group", ["admins", "managers"])
    def test_the_recs_own_managers_and_admins_may_read_it(
        self, client, registry, operator_token, group
    ):
        """
        @verifies REQ-0015
        """
        token = operator_token(ORG, group)
        response = client.get(DRIFT, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

    @pytest.mark.parametrize("group", ["editors", "viewers"])
    def test_the_recs_editors_and_viewers_may_not(self, client, registry, operator_token, group):
        """D55: not organisation viewers, and not editors either.

        @verifies REQ-0015
        """
        token = operator_token(ORG, group)
        response = client.get(DRIFT, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403
        assert registry.requests == []

    @pytest.mark.parametrize("group", ["managers", "editors", "viewers"])
    def test_no_realm_group_but_admins_may(self, client, registry, issue_token, group):
        """
        @verifies REQ-0015
        """
        token = issue_token(sub="op", email="op@example.org", groups=[f"/{group}"])
        response = client.get(DRIFT, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403
        assert registry.requests == []

    @pytest.mark.parametrize("scopes", [("onboarding.admin",), ("onboarding.recs.read",)])
    def test_no_service_account_may(self, client, registry, service_token, scopes):
        """
        @verifies REQ-0015
        """
        token = service_token(*scopes)
        response = client.get(DRIFT, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403
        assert registry.requests == []

    def test_the_console_learns_it_from_me(self, client, registry, operator_token, realm_admin):
        """The console shows its *Areas* page only to who holds `recs.drift`.

        @verifies REQ-0015
        """

        def _caps(headers) -> list[str]:
            [rec] = client.get("/api/admin/me", headers=headers).json()["recs"]
            return rec["capabilities"]

        assert "recs.drift" in _caps(realm_admin)
        manager = operator_token(ORG, "managers")
        assert "recs.drift" in _caps({"Authorization": f"Bearer {manager}"})
        viewer = operator_token(ORG, "viewers")
        assert "recs.drift" not in _caps({"Authorization": f"Bearer {viewer}"})

    def test_another_organization_may_not(self, client, registry, operator_token):
        """
        @verifies REQ-0015
        """
        token = operator_token("other-rec", "admins")
        response = client.get(DRIFT, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403

    def test_a_municipality_template_is_not_synced(self, client, registry, realm_admin, seed_rec):
        """
        @verifies REQ-0015
        """
        seed_rec(
            REC,
            organization=ORG,
            rec_registry={"community": COMMUNITY, "default_area": "north", "areas": {}},
        )
        body = client.get(DRIFT, headers=realm_admin).json()
        assert body["status"] == "not_synced"
        assert registry.requests == []


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


def test_nothing_personal_is_logged(client, registry, provisioning, realm_admin, caplog):
    """Keys, boundary ids and counts only; a member is counted, never named.

    @verifies REQ-0012
    """
    caplog.set_level(logging.DEBUG)
    registry.seed_area("east", "AC000E00003")
    registry.add_member("east", 2)
    _sync(client, realm_admin, prune=True)
    assert "Example Person" not in caplog.text
    assert "m-0000" not in caplog.text
    assert "platform-admin@example.org" not in caplog.text


# ---------------------------------------------------------------------------
# The CLI (REQ-0009): the same route, as a person, or in process
# ---------------------------------------------------------------------------

API = "http://onboarding.test"


@pytest.fixture()
def cli_to_app(client, monkeypatch):
    """`onboarding-cli`'s HTTP transport, delivered to the app under test.

    The token the CLI sends is verified for real by the app, and the app's
    route, policy and service layer answer; only the socket is replaced.
    """
    from celine.onboarding.cli import transport
    from celine.onboarding.config.settings import settings

    monkeypatch.setattr(settings, "onboarding_api_url", API)
    original = transport.ApiTransport.__init__

    def _init(self, base_url=None, token=None):
        original(self, base_url, token)
        self._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app))

    monkeypatch.setattr(transport.ApiTransport, "__init__", _init)


def _cli(*args: str):
    from typer.testing import CliRunner

    from celine.onboarding.cli.main import app

    return CliRunner().invoke(app, ["registry-sync", "--rec", REC, *args])


class TestTheCli:
    def test_without_a_token_or_local_it_refuses_before_asking(
        self, cli_to_app, registry, provisioning
    ):
        """The CLI's own service account never starts a sync (D35).

        @verifies REQ-0009
        """
        result = _cli()
        assert result.exit_code == 2
        assert "--token" in result.output and "--local" in result.output
        assert registry.requests == [] and provisioning.calls == []

    def test_a_non_admin_token_is_refused(self, cli_to_app, registry, operator_token):
        """
        @verifies REQ-0009
        """
        result = _cli("--token", operator_token(ORG, "admins"))
        assert result.exit_code == 1
        assert "Not permitted" in result.output
        assert registry.requests == []

    def test_a_service_token_is_refused(self, cli_to_app, registry, service_token):
        """
        @verifies REQ-0009
        """
        result = _cli("--token", service_token("onboarding.admin"))
        assert result.exit_code == 1
        assert "Not permitted" in result.output
        assert registry.requests == []

    def test_a_realm_admin_token_syncs(self, cli_to_app, registry, provisioning, realm_admin):
        """
        @verifies REQ-0009
        """
        token = realm_admin["Authorization"].removeprefix("Bearer ")
        result = _cli("--token", token)
        assert result.exit_code == 0, result.output
        assert "set up community: succeeded" in result.output
        assert set(registry.snapshot()["areas"]) == {"north", "south"}

    def test_a_dry_run_writes_nothing(self, cli_to_app, registry, provisioning, realm_admin):
        """
        @verifies REQ-0010
        """
        token = realm_admin["Authorization"].removeprefix("Bearer ")
        result = _cli("--token", token, "--dry-run", "--json")
        assert result.exit_code == 0, result.output
        report = json.loads(result.output)
        assert report["dry_run"] is True
        assert registry.writes() == [] and provisioning.calls == []

    def test_a_refused_area_exits_1(self, cli_to_app, registry, provisioning, realm_admin):
        """
        @verifies REQ-0012
        """
        registry.seed_area("east", "AC000E00003")
        registry.add_member("east", 2)
        token = realm_admin["Authorization"].removeprefix("Bearer ")
        result = _cli("--token", token, "--prune")
        assert result.exit_code == 1
        assert "members 2" in result.output

    def test_an_invalid_template_is_said(self, cli_to_app, registry, realm_admin, template):
        """
        @verifies REQ-0013
        """
        template(north="AC000E00009")
        token = realm_admin["Authorization"].removeprefix("Bearer ")
        result = _cli("--token", token)
        assert result.exit_code == 1
        assert "AC000E00009" in result.output and "[template_invalid]" in result.output

    def test_local_is_off_unless_allowed(self, registry, template, monkeypatch):
        """
        @verifies REQ-0009
        """
        from celine.onboarding.config.settings import settings

        monkeypatch.setattr(settings, "allow_local_admin", False)
        result = _cli("--local")
        assert result.exit_code == 1
        assert "ALLOW_LOCAL_ADMIN" in result.output
        assert registry.requests == []

    def test_local_runs_in_process_and_is_audited_as_the_cli(
        self, registry, provisioning, template, monkeypatch
    ):
        """
        @verifies REQ-0009
        """
        from celine.onboarding.cli import transport
        from celine.onboarding.config.settings import settings

        monkeypatch.setattr(settings, "allow_local_admin", True)
        session = RecordingSession()

        class _Session:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *exc):
                return False

        async def _no_database(self):
            return _Session()

        monkeypatch.setattr(transport.LocalTransport, "_session", _no_database)

        result = _cli("--local")

        assert result.exit_code == 0, result.output
        assert set(registry.snapshot()["areas"]) == {"north", "south"}
        [row] = session.audit_rows
        assert row.action == "registry_sync" and row.actor_type == "cli"
