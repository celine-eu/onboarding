"""Registering an approved participant as a community member.

The third effect of approval, and the one that was missing: a Keycloak user and
a dataspace identity were provisioned, but nothing wrote the member into the
registry. An approved participant was therefore enabled in name only — invisible
to every pipeline, dashboard and digital-twin query, all of which join on the
registry's `user_id`, POD and sensor ids.

Unlike share provisioning this step **fails closed**. A missing consent row is
recoverable; a member who does not exist is not a state anything downstream can
work around.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from celine.onboarding.services import rec_registry as rr
from celine.onboarding.services import template_service as ts


def _sub(**overrides):
    base = dict(
        ref="20260727-abcd",
        rec_slug="example",
        first_name="Alice",
        last_name="Rossi",
        pod_code="IT001E00000001",
        extra_data={},
        extracted_data={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


BINDING = ts.RecRegistryBinding(community="test-community", area="north")


# ── the binding ───────────────────────────────────────────────────────────────


class TestBinding:
    def test_no_block_means_no_registration(self, bind_rec):
        bind_rec("plain")
        assert ts.rec_registry_binding("plain").enabled is False

    def test_block_resolves(self, bind_rec):
        manifest = bind_rec("rec-a")
        manifest["rec_registry"] = {"community": "rec-a", "area": "north"}
        binding = ts.rec_registry_binding("rec-a")

        assert binding.enabled is True
        assert binding.community == "rec-a"
        assert binding.area == "north"

    def test_community_is_required(self):
        with pytest.raises(ValueError, match="community' is required"):
            ts.validate_rec_registry_block({"area": "north"}, where="test")

    def test_area_is_required(self):
        """Assigning a member to the right area needs the community's areas to be
        1-1 with the registry's *and* geocoding against their geofences. Until
        that lands there is one configured area, asked for rather than guessed —
        a wrong area is sticky, since the registry refuses to delete one while
        members reference it."""
        with pytest.raises(ValueError, match="area' is required"):
            ts.validate_rec_registry_block({"community": "rec-a"}, where="test")

    def test_absent_block_validates(self):
        ts.validate_rec_registry_block(None, where="test")


# ── payload building ──────────────────────────────────────────────────────────


class TestPayload:
    def test_pod_becomes_a_delivery_point(self):
        payload = rr.build_member_payload(_sub(), BINDING)

        assert payload["delivery_points"] == [
            {
                "id": "IT001E00000001",
                "type": "pod",
                "description": "Supply point declared at onboarding",
                "active": True,
            }
        ]

    def test_no_pod_means_no_delivery_points(self):
        payload = rr.build_member_payload(_sub(pod_code=None), BINDING)
        assert payload["delivery_points"] == []

    def test_name_is_assembled_from_the_parts(self):
        assert rr.build_member_payload(_sub(), BINDING)["name"] == "Alice Rossi"

    def test_missing_name_falls_back_to_the_reference(self):
        """A member row needs a name; the submission reference is the honest
        placeholder rather than an empty string."""
        payload = rr.build_member_payload(
            _sub(first_name=None, last_name=None), BINDING
        )
        assert payload["name"] == "20260727-abcd"

    def test_area_is_the_configured_one(self):
        """Not derived from the supply address: per-member assignment needs
        geocoding against the community's geofences, which is not wired yet.
        A REC manager moves people until it is."""
        payload = rr.build_member_payload(
            _sub(extracted_data={"comune": "Somewhere"}), BINDING
        )
        assert payload["area"] == "north"


class TestRole:
    def test_pv_makes_a_prosumer(self):
        assert rr.member_role(_sub(extra_data={"has_pv": True})) == "prosumer"

    def test_no_pv_makes_a_consumer(self):
        assert rr.member_role(_sub(extra_data={"has_pv": False})) == "consumer"

    def test_a_community_that_does_not_ask_gets_consumer(self):
        """The safe reading: claiming somebody produces when they do not would
        put them in the wrong settlement group."""
        assert rr.member_role(_sub(extra_data={})) == "consumer"


class TestNoAssetsAreRegistered:
    """Self-stated answers are declarations, not commissioned installations.

    Registering them as assets would make an unverified claim indistinguishable
    from a surveyed one. Asset registration is the REC manager's offline work,
    and a meter cannot be registered at onboarding at all — its `sensor_id` is
    assigned when the device is physically installed.
    """

    def test_declared_pv_creates_no_asset(self):
        payload = rr.build_member_payload(
            _sub(extra_data={"has_pv": True, "pv_kwp": 4.5}), BINDING
        )
        assert payload["assets"] == {}

    def test_declared_battery_creates_no_asset(self):
        payload = rr.build_member_payload(
            _sub(extra_data={"has_battery": True, "battery_kwh": 10}), BINDING
        )
        assert payload["assets"] == {}

    def test_the_declarations_are_kept(self):
        """A REC manager works from them when deciding what to survey, so losing
        them between the wizard and the registry would mean asking again."""
        payload = rr.build_member_payload(
            _sub(
                extra_data={
                    "has_pv": True,
                    "pv_kwp": 4.5,
                    "has_ev": True,
                    "has_heat_pump": False,
                }
            ),
            BINDING,
        )

        declared = payload["extra"]["declared_at_onboarding"]
        assert declared["pv_kwp"] == 4.5
        assert declared["has_ev"] is True
        assert declared["has_heat_pump"] is False

    def test_the_pod_is_still_tracked(self):
        """The one thing that must come from onboarding: the distributor keys on
        it, metering data arrives against it, and unlike a meter it is known
        before any device is installed."""
        payload = rr.build_member_payload(_sub(), BINDING)
        assert payload["delivery_points"][0]["id"] == "IT001E00000001"


# ── registration ──────────────────────────────────────────────────────────────


@pytest.fixture()
def _configured(monkeypatch, bind_rec):
    manifest = bind_rec("example")
    manifest["rec_registry"] = {"community": "test-community", "area": "north"}
    monkeypatch.setattr(rr.settings, "rec_registry_url", "http://registry:8004")
    rr._client = None
    yield
    rr._client = None


def _stub_client(monkeypatch, status: int, content: bytes = b""):
    calls: list = []

    class _Client:
        async def create_member(self, community, body):
            calls.append((community, body))
            return SimpleNamespace(status_code=status, content=content)

    monkeypatch.setattr(rr, "_get_client", lambda: _Client())
    return calls


class TestRegisterMember:
    async def test_skipped_without_a_registry_url(self, monkeypatch, bind_rec):
        bind_rec("example")
        monkeypatch.setattr(rr.settings, "rec_registry_url", "")
        assert await rr.register_member(_sub()) is None

    async def test_skipped_without_a_binding(self, monkeypatch, bind_rec):
        """A community with no rec_registry block is a supported configuration."""
        bind_rec("example")
        monkeypatch.setattr(rr.settings, "rec_registry_url", "http://registry:8004")
        assert await rr.register_member(_sub()) is None

    async def test_registers_and_returns_the_key(self, monkeypatch, _configured):
        calls = _stub_client(monkeypatch, 201)

        key = await rr.register_member(_sub())

        assert key == "20260727-abcd"
        assert calls[0][0] == "test-community"

    async def test_already_registered_is_not_a_failure(self, monkeypatch, _configured):
        """Approval is retriable. Refusing the second attempt would leave a
        submission that can never be approved."""
        _stub_client(monkeypatch, 409, b"already exists")

        assert await rr.register_member(_sub()) == "20260727-abcd"

    async def test_refusal_fails_closed(self, monkeypatch, _configured):
        """Unlike share provisioning: a member who does not exist is invisible to
        everything downstream, and no retry path recovers a silent skip."""
        _stub_client(monkeypatch, 422, b"area 'north' unknown")

        with pytest.raises(ValueError, match="REC registry refused"):
            await rr.register_member(_sub())

    async def test_the_refusal_says_what_the_registry_said(
        self, monkeypatch, _configured
    ):
        _stub_client(monkeypatch, 422, b"area 'north' unknown")

        with pytest.raises(ValueError, match="area 'north' unknown"):
            await rr.register_member(_sub())
