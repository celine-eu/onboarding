"""The console's list of PODs more than one active member holds (registry plan F8).

The registry reports, per community, each point an active member holds that
another active member also holds: this community's holders by member key, others
as a count only. The route links each holder to the submission it came from,
masks the PODs unless the operator reveals them (audited), and turns the
registry's refusals into answers an operator can read.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from celine.sdk.rec_registry import RecRegistryApiError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from celine.onboarding.api.admin import create_admin_router
from celine.onboarding.config.settings import settings
from celine.onboarding.security.middleware import AdminAuthMiddleware
from celine.onboarding.services import audit_service, rec_registry, template_service

ORG = "community-a"
URL = "/api/admin/rec-a/delivery-points/shared"
SUB_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
SHARED = "IT001E00000001"


REPORT = {
    "community_key": "example-rec",
    "items": [
        {
            "delivery_point": SHARED.lower(),
            "holders": [
                {"member_key": "20261001-aaaa", "id": SHARED},
                {"member_key": "imported-member", "id": SHARED.lower()},
            ],
            "held_elsewhere": 1,
            "active_holders": 3,
        }
    ],
}


def report(items=None):
    """The SDK's own schema, so the route is read against the real field names."""
    from celine.sdk.openapi.rec_registry.schemas import DeliveryPointDuplicatesSchema

    body = dict(REPORT) if items is None else {**REPORT, "items": items}
    return DeliveryPointDuplicatesSchema.model_validate(body)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.queries = 0

    async def execute(self, _query):
        self.queries += 1
        return _Result(self.rows)

    async def commit(self):
        pass


class FakeRegistry:
    def __init__(self):
        self.answer = report()
        self.error: Exception | None = None
        self.asked: list[str] = []

    async def list_duplicate_delivery_points(self, community_key, *, token=None):
        self.asked.append(community_key)
        if self.error is not None:
            raise self.error
        return self.answer


@pytest.fixture()
def world(monkeypatch, seed_rec):
    seed_rec("rec-a", name="REC A", organization=ORG, steps=["consents", "personal", "review"])
    registry = FakeRegistry()
    trail: list[dict] = []

    async def _record(db, **kw):
        trail.append(kw)

    monkeypatch.setattr(audit_service, "record_and_commit", _record)
    monkeypatch.setattr(rec_registry, "_get_client", lambda: registry)
    monkeypatch.setattr(settings, "rec_registry_url", "http://registry:8000")
    monkeypatch.setattr(
        template_service,
        "rec_registry_binding",
        lambda slug: SimpleNamespace(enabled=True, community="example-rec"),
    )

    from celine.onboarding.models.database import get_db

    db = FakeDb([SimpleNamespace(id=SUB_ID, ref="20261001-aaaa", rec_slug="rec-a")])
    app = FastAPI()
    app.add_middleware(AdminAuthMiddleware)
    app.include_router(create_admin_router())

    async def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    return SimpleNamespace(client=TestClient(app), registry=registry, trail=trail, db=db)


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_each_holder_is_linked_to_its_submission_and_the_pods_are_masked(world, operator_token):
    res = world.client.get(URL, headers=auth(operator_token(ORG, "managers")))

    assert res.status_code == 200, res.text
    body = res.json()
    assert world.registry.asked == ["example-rec"]
    assert (body["community_key"], body["revealed"]) == ("example-rec", False)
    item = body["items"][0]
    assert item["delivery_point"] == "••••••••••0001"
    assert (item["held_elsewhere"], item["active_holders"]) == (1, 3)
    onboarded, imported = item["holders"]
    assert onboarded == {
        "member_key": "20261001-aaaa",
        "id": "••••••••••0001",
        "submission_id": str(SUB_ID),
        "submission_ref": "20261001-aaaa",
    }
    # A member the registry holds that did not come through this service.
    assert (imported["submission_id"], imported["submission_ref"]) == (None, None)
    assert SHARED not in res.text and SHARED.lower() not in res.text
    assert world.trail[-1]["action"] == "view"
    assert world.trail[-1]["detail"] == "points=1"


def test_reveal_unmasks_and_is_audited(world, operator_token):
    res = world.client.get(f"{URL}?reveal=true", headers=auth(operator_token(ORG, "admins")))

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["revealed"] is True
    assert body["items"][0]["delivery_point"] == SHARED.lower()
    assert [h["id"] for h in body["items"][0]["holders"]] == [SHARED, SHARED.lower()]
    entry = world.trail[-1]
    assert (entry["action"], entry["entity_type"]) == ("reveal", "shared_delivery_points")
    assert SHARED not in entry["detail"] and SHARED.lower() not in entry["detail"]


@pytest.mark.parametrize(("group", "status"), [("editors", 403), ("viewers", 403)])
def test_it_needs_the_revise_capability(world, operator_token, group, status):
    res = world.client.get(URL, headers=auth(operator_token(ORG, group)))
    assert res.status_code == status
    assert world.registry.asked == []


def test_reveal_without_the_reveal_capability_is_refused(world, operator_token, monkeypatch):
    from celine.onboarding.security import policy

    real = policy.OnboardingAccessPolicy.allow

    def _no_reveal(self, user, capability, **kw):
        if capability == policy.Capability.SUBMISSIONS_REVEAL:
            return policy.Decision(False, "no reveal")
        return real(self, user, capability, **kw)

    monkeypatch.setattr(policy.OnboardingAccessPolicy, "allow", _no_reveal)

    res = world.client.get(f"{URL}?reveal=true", headers=auth(operator_token(ORG, "managers")))

    assert res.status_code == 403
    assert world.registry.asked == []


def test_nothing_shared_is_an_empty_list(world, operator_token):
    world.registry.answer = report(items=[])

    res = world.client.get(URL, headers=auth(operator_token(ORG, "managers")))

    assert res.status_code == 200
    assert res.json()["items"] == []
    assert world.db.queries == 0


def test_a_community_the_registry_does_not_hold_is_a_404(world, operator_token):
    world.registry.error = RecRegistryApiError(
        "list-duplicate-delivery-points: Community not found", status_code=404
    )

    res = world.client.get(URL, headers=auth(operator_token(ORG, "managers")))

    assert res.status_code == 404
    assert "holds no community" in res.json()["detail"]


def test_another_registry_refusal_is_a_502_without_its_text(world, operator_token):
    world.registry.error = RecRegistryApiError("refused: member ex-00001 holds it", status_code=500)

    res = world.client.get(URL, headers=auth(operator_token(ORG, "managers")))

    assert res.status_code == 502
    assert "ex-00001" not in res.text


def test_an_unreachable_registry_is_a_503(world, operator_token):
    world.registry.error = httpx.ConnectError("refused")

    res = world.client.get(URL, headers=auth(operator_token(ORG, "managers")))

    assert res.status_code == 503


def test_no_registry_for_this_community_is_a_409(world, operator_token, monkeypatch):
    monkeypatch.setattr(settings, "rec_registry_url", "")

    res = world.client.get(URL, headers=auth(operator_token(ORG, "managers")))

    assert res.status_code == 409
    assert world.registry.asked == []


def test_through_the_sdk_client_a_404_is_read_as_no_community(world, operator_token, monkeypatch):
    """The real `RecRegistryAdminClient` over a mocked registry: the route, the
    path it calls, and the registry's uncoded 404 for an unknown community."""
    from celine.sdk.rec_registry import RecRegistryAdminClient

    seen: list[str] = []
    original = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/admin/communities/example-rec/delivery-points/duplicates"):
            return httpx.Response(404, json={"detail": "Community not found"})
        raise AssertionError(f"unexpected call {request.url}")

    def factory(**kw):
        kw.pop("transport", None)
        return original(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    client = RecRegistryAdminClient(base_url="http://registry:8000", default_token="t")
    monkeypatch.setattr(rec_registry, "_get_client", lambda: client)

    res = world.client.get(URL, headers=auth(operator_token(ORG, "managers")))

    assert seen == ["/admin/communities/example-rec/delivery-points/duplicates"]
    assert res.status_code == 404
    assert "holds no community" in res.json()["detail"]
