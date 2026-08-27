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
        email="Alice.Rossi@example.org",
        first_name="Alice",
        last_name="Rossi",
        pod_code="IT001E00000001",
        supply_municipality=None,
        extra_data={},
        extracted_data={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


BINDING = ts.RecRegistryBinding(
    community="test-community",
    default_area="north",
    areas={"valley-north": ["Springfield", "Shelbyville"]},
)


# ── the binding ───────────────────────────────────────────────────────────────


class TestBinding:
    def test_no_block_means_no_registration(self, bind_rec):
        bind_rec("plain")
        assert ts.rec_registry_binding("plain").enabled is False

    def test_block_resolves(self, bind_rec):
        manifest = bind_rec("rec-a")
        manifest["rec_registry"] = {
            "community": "rec-a",
            "default_area": "north",
            "areas": {"valley-north": ["Springfield"]},
        }
        binding = ts.rec_registry_binding("rec-a")

        assert binding.enabled is True
        assert binding.community == "rec-a"
        assert binding.default_area == "north"
        assert binding.areas == {"valley-north": ["Springfield"]}

    def test_community_is_required(self):
        with pytest.raises(ValueError, match="community' is required"):
            ts.validate_rec_registry_block({"default_area": "north"}, where="test")

    def test_default_area_is_required(self):
        """A member with no area cannot be registered at all, so there has to be
        somewhere to put one whose municipality matches nothing."""
        with pytest.raises(ValueError, match="default_area' is required"):
            ts.validate_rec_registry_block({"community": "rec-a"}, where="test")

    def test_areas_must_map_to_lists(self):
        with pytest.raises(ValueError, match="must be a list of"):
            ts.validate_rec_registry_block(
                {"community": "c", "default_area": "n", "areas": {"a": "Springfield"}},
                where="test",
            )

    def test_a_municipality_claimed_twice_is_refused(self):
        """Otherwise a member's area depends on declaration order — an authoring
        mistake, not a policy, so it fails at import."""
        with pytest.raises(ValueError, match="claimed by both"):
            ts.validate_rec_registry_block(
                {
                    "community": "c",
                    "default_area": "n",
                    "areas": {"a": ["Springfield"], "b": ["springfield"]},
                },
                where="test",
            )


class TestAreaResolution:
    """A coarse stand-in for geofences: municipality lists per area.

    Broad on purpose. Matching a municipality is not resolving a point against a
    polygon, and it is wrong for a member whose address sits in a municipality
    split across two areas — right often enough to be worth doing, with a REC
    manager moving the rest.
    """

    def test_a_covered_municipality_picks_its_area(self):
        assert BINDING.area_for("Springfield") == "valley-north"

    def test_matching_ignores_case_and_padding(self):
        """The name arrives from OCR of a bill, not from a picker."""
        assert BINDING.area_for("  shelbyville ") == "valley-north"

    def test_an_uncovered_municipality_falls_back(self):
        assert BINDING.area_for("Ogdenville") == "north"

    def test_no_municipality_falls_back(self):
        assert BINDING.area_for(None) == "north"

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
        payload = rr.build_member_payload(_sub(first_name=None, last_name=None), BINDING)
        assert payload["name"] == "20260727-abcd"

    def test_the_geocoded_municipality_wins(self):
        """A geocoder returns the municipality as its own field; a bill states a
        full address as free text and OCR of it is a guess."""
        payload = rr.build_member_payload(
            _sub(
                supply_municipality="Springfield",
                extracted_data={"comune": "Ogdenville"},
            ),
            BINDING,
        )
        assert payload["area"] == "valley-north"

    def test_area_comes_from_the_extracted_municipality(self):
        payload = rr.build_member_payload(_sub(extracted_data={"comune": "Springfield"}), BINDING)
        assert payload["area"] == "valley-north"

    def test_an_uncovered_municipality_uses_the_default(self):
        payload = rr.build_member_payload(_sub(extracted_data={"comune": "Somewhere"}), BINDING)
        assert payload["area"] == "north"

    def test_no_extraction_uses_the_default(self):
        assert rr.build_member_payload(_sub(), BINDING)["area"] == "north"

    def test_the_address_is_not_substring_matched(self):
        """Italian street names routinely contain other municipalities' names:
        substring-matching "Via Roma 1, Lavarone" against a municipality list
        would file the member under Roma."""
        payload = rr.build_member_payload(
            _sub(extracted_data={"indirizzo": "Via Springfield 1, Ogdenville"}),
            BINDING,
        )
        assert payload["area"] == "north"


class TestUserId:
    """`user_id` is what the participant authenticates as — issue #1.

    The registry resolves a self-service caller by matching `Member.user_id`
    against the token's `preferred_username`. Writing anything else produces a
    member row that is correct in every listing, exports and round-trips cleanly,
    and whose owner is told `403 You are not a member of any community`.
    """

    def test_the_keycloak_username_is_written(self):
        payload = rr.build_member_payload(_sub(), BINDING, keycloak_username="gl-00001")
        assert payload["user_id"] == "gl-00001"

    def test_the_key_stays_the_reference(self):
        """The two identifiers are different on purpose: the key is the
        registry's handle on the member, the user_id is who they log in as."""
        payload = rr.build_member_payload(_sub(), BINDING, keycloak_username="gl-00001")
        assert payload["key"] == "20260727-abcd"

    def test_the_reference_is_never_the_user_id(self):
        """What the bug was."""
        payload = rr.build_member_payload(_sub(), BINDING)
        assert payload["user_id"] != payload["key"]

    def test_the_email_is_the_fallback(self):
        """It is the username this service sets on every user it creates, so it
        is right for all of them — and it is what a retry of registration alone
        has, since that does not re-run provisioning."""
        payload = rr.build_member_payload(_sub(), BINDING)
        assert payload["user_id"] == "alice.rossi@example.org"

    def test_the_fallback_is_normalised_the_way_provisioning_normalises_it(self):
        payload = rr.build_member_payload(_sub(email="  Alice.Rossi@Example.org "), BINDING)
        assert payload["user_id"] == "alice.rossi@example.org"

    def test_a_reported_username_beats_the_email(self):
        """A user provisioning found rather than created can log in under a name
        that is not their email, and asking by email says nothing about what
        their token will carry."""
        payload = rr.build_member_payload(
            _sub(email="alice@example.org"), BINDING, keycloak_username="gl-00001"
        )
        assert payload["user_id"] == "gl-00001"

    def test_no_username_and_no_email_falls_back_to_the_reference(self, caplog):
        """The broken value, kept because there is nothing better — and logged,
        so that a member who cannot resolve themselves is not silent."""
        with caplog.at_level("WARNING"):
            payload = rr.build_member_payload(_sub(email=None), BINDING)

        assert payload["user_id"] == "20260727-abcd"
        assert "cannot resolve" in caplog.text


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
        payload = rr.build_member_payload(_sub(extra_data={"has_pv": True, "pv_kwp": 4.5}), BINDING)
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
    manifest["rec_registry"] = {
        "community": "test-community",
        "default_area": "north",
    }
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

    async def test_the_refusal_says_what_the_registry_said(self, monkeypatch, _configured):
        _stub_client(monkeypatch, 422, b"area 'north' unknown")

        with pytest.raises(ValueError, match="area 'north' unknown"):
            await rr.register_member(_sub())


class TestRegisterMemberUserId:
    async def test_the_username_reaches_the_registry(self, monkeypatch, _configured):
        calls = _stub_client(monkeypatch, 201)

        await rr.register_member(_sub(), keycloak_username="gl-00001")

        assert calls[0][1].to_dict()["user_id"] == "gl-00001"

    async def test_without_one_the_email_reaches_the_registry(self, monkeypatch, _configured):
        calls = _stub_client(monkeypatch, 201)

        await rr.register_member(_sub())

        assert calls[0][1].to_dict()["user_id"] == "alice.rossi@example.org"
