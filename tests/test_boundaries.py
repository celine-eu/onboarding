"""The Digital Twin client and template import for primary-substation boundaries.

Integration-level: `respx` stands in front of the Digital Twin and the real
celine-sdk `DTClient` makes the calls, so the path, the token and the payload
asserted here are what production sends. The fake decides containment on
synthetic squares as the Digital Twin does (D28; see `fake_digital_twin.py`).
"""

from __future__ import annotations

import logging

import httpx
import pytest
from fake_digital_twin import (
    INSIDE_1,
    INSIDE_2,
    ON_SHARED_EDGE,
    OUTSIDE_ALL,
    SERVICE_TOKEN,
    boundary_manifest,
)

from celine.onboarding.services import boundaries
from celine.onboarding.services import template_service as ts
from celine.onboarding.services.boundaries import DigitalTwinUnavailableError

SOURCE = "gse_cabine_primarie"


def _without_slug(manifest: dict) -> dict:
    return {k: v for k, v in manifest.items() if k != "slug"}


class TestBoundaryAtPoint:
    async def test_a_point_inside_answers_its_boundary(self, fake_dt):
        """
        @verifies REQ-0003
        """
        lat, lon = INSIDE_2
        assert await boundaries.boundary_at_point(SOURCE, lat, lon, community="example-rec") == (
            "AC000E00002"
        )

    async def test_a_point_outside_every_shape_answers_none(self, fake_dt):
        """
        @verifies REQ-0003
        """
        lat, lon = OUTSIDE_ALL
        assert await boundaries.boundary_at_point(SOURCE, lat, lon, community="example-rec") is None

    async def test_a_point_on_a_shared_edge_is_inside_the_lowest_id(self, fake_dt):
        """D28: an edge is inside, and two covering shapes resolve to the lowest id.

        @verifies REQ-0003
        """
        lat, lon = ON_SHARED_EDGE
        assert await boundaries.boundary_at_point(SOURCE, lat, lon, community="example-rec") == (
            "AC000E00001"
        )

    async def test_the_request_is_this_services_own_and_carries_only_the_point(self, fake_dt):
        """
        @verifies REQ-0006
        """
        lat, lon = INSIDE_1
        await boundaries.boundary_at_point(SOURCE, lat, lon, community="example-rec")

        [(community, fetcher, payload, auth)] = fake_dt.calls()
        assert community == "example-rec"
        assert fetcher == "boundary_at_point"
        assert payload == {"source": SOURCE, "lat": lat, "lon": lon}
        assert auth == f"Bearer {SERVICE_TOKEN}"

    async def test_an_unknown_source_is_refused_by_the_digital_twin_and_fails_closed(self, fake_dt):
        """The Digital Twin answers 400 for a source outside its closed enum; that
        is no answer, never a `None` that would read as "outside".

        @verifies REQ-0005
        """
        lat, lon = INSIDE_1
        with pytest.raises(DigitalTwinUnavailableError):
            await boundaries.boundary_at_point("nuts_regions", lat, lon, community="example-rec")

    @pytest.mark.parametrize(
        "failure",
        [
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("timed out"),
            401,
            403,
            500,
            503,
        ],
        ids=["unreachable", "timeout", "token-refused", "forbidden", "error", "unavailable"],
    )
    async def test_every_failure_to_answer_is_one_typed_failure(self, fake_dt, failure):
        """
        @verifies REQ-0005
        """
        fake_dt.failure = failure
        lat, lon = INSIDE_1
        with pytest.raises(DigitalTwinUnavailableError):
            await boundaries.boundary_at_point(SOURCE, lat, lon, community="example-rec")

    async def test_no_configured_digital_twin_fails_closed_without_a_call(
        self, fake_dt, monkeypatch
    ):
        """
        @verifies REQ-0005
        """
        monkeypatch.setattr(boundaries.settings, "digital_twin_url", "")
        with pytest.raises(DigitalTwinUnavailableError):
            await boundaries.boundary_at_point(SOURCE, *INSIDE_1, community="example-rec")
        assert fake_dt.calls() == []

    async def test_a_row_without_an_id_is_no_answer(self, fake_dt):
        fake_dt.override = lambda fetcher, payload: httpx.Response(
            200, json={"items": [{"geojson": "{}"}], "count": 1, "limit": 1, "offset": 0}
        )
        with pytest.raises(DigitalTwinUnavailableError):
            await boundaries.boundary_at_point(SOURCE, *INSIDE_1, community="example-rec")

    async def test_the_point_is_never_logged_nor_carried_by_the_failure(self, fake_dt, caplog):
        """The Digital Twin's refusal body echoes the payload; neither the log nor
        the exception may carry it.

        @verifies REQ-0007
        """
        caplog.set_level(logging.DEBUG)
        fake_dt.failure = 500
        lat, lon = 0.012345, 0.054321
        with pytest.raises(DigitalTwinUnavailableError) as raised:
            await boundaries.boundary_at_point(SOURCE, lat, lon, community="example-rec")

        text = caplog.text + str(raised.value)
        assert "0.012345" not in text
        assert "0.054321" not in text


class TestKnownBoundaryIds:
    async def test_an_unknown_id_is_absent(self, fake_dt):
        """
        @verifies REQ-0002
        """
        known = await boundaries.known_boundary_ids(
            SOURCE, ["AC000E00001", "AC000E00009"], community="example-rec"
        )
        assert known == {"AC000E00001"}

    async def test_more_ids_than_one_call_takes_are_asked_in_batches(self, fake_dt):
        ids = ["AC000E00001"] + [f"ex-{n:05d}" for n in range(150)]
        known = await boundaries.known_boundary_ids(SOURCE, ids, community="example-rec")

        assert known == {"AC000E00001"}
        batches = [payload["ids"] for _, _, payload, _ in fake_dt.calls("boundary_shape")]
        assert [len(b) for b in batches] == [100, 51]


# ── template import ──────────────────────────────────────────────────────────


class TestTemplateStructure:
    def test_a_boundary_template_resolves_to_its_areas(self, seed_rec):
        """
        @verifies REQ-0001
        """
        seed_rec(
            "rec-b",
            **_without_slug(boundary_manifest("rec-b", north="AC000E00001", south="AC000E00002")),
        )
        binding = ts.rec_registry_binding("rec-b")

        assert binding.uses_boundaries
        assert binding.community == "example-rec"
        assert binding.area_for_boundary(SOURCE, "AC000E00002") == "south"
        assert binding.area_for_boundary(SOURCE, "AC000E00003") is None
        assert binding.default_area == ""

    def test_a_boundary_template_has_no_municipality_fallback(self, seed_rec):
        """
        @verifies REQ-0001
        """
        seed_rec("rec-b", **_without_slug(boundary_manifest("rec-b", north="AC000E00001")))
        with pytest.raises(ValueError, match="never a municipality"):
            ts.rec_registry_binding("rec-b").area_for("Springfield")

    @pytest.mark.parametrize(
        ("areas", "message"),
        [
            (
                {"north": {"boundary": {"source": "nuts_regions", "id": "AC000E00001"}}},
                "must be one of",
            ),
            ({"north": {"boundary": {"source": SOURCE}}}, "boundary.id"),
            ({"north": {"boundary": {"source": SOURCE, "id": ""}}}, "boundary.id"),
            ({"north": {"boundary": {"source": SOURCE, "id": " AC000E00001"}}}, "boundary.id"),
            ({"north": {"boundary": {"source": SOURCE, "id": "A" * 65}}}, "boundary.id"),
            ({"north": {"boundary": {"source": SOURCE, "id": 7}}}, "boundary.id"),
            ({"north": {"boundary": "AC000E00001"}}, "must be a mapping"),
            (
                {"north": {"boundary": {"source": SOURCE, "id": "AC000E00001", "shape": {}}}},
                "unknown key",
            ),
            (
                {"north": {"boundary": {"source": SOURCE, "id": "AC000E00001"}, "extra": 1}},
                "unknown key",
            ),
            (
                {
                    "north": {"boundary": {"source": SOURCE, "id": "AC000E00001"}},
                    "south": ["Springfield"],
                },
                "mixes boundaries",
            ),
        ],
        ids=[
            "unknown-source",
            "no-id",
            "empty-id",
            "padded-id",
            "id-too-long",
            "id-not-a-string",
            "boundary-not-a-mapping",
            "unknown-boundary-key",
            "unknown-area-key",
            "mixed-with-municipalities",
        ],
    )
    def test_a_malformed_boundary_is_refused(self, areas, message):
        """
        @verifies REQ-0001
        """
        with pytest.raises(ValueError, match=message):
            ts.validate_rec_registry_block({"community": "example-rec", "areas": areas}, where="t")

    def test_a_default_area_beside_boundaries_is_refused(self):
        """
        @verifies REQ-0001
        """
        block = boundary_manifest("rec-b", north="AC000E00001")["rec_registry"]
        block["default_area"] = "north"
        with pytest.raises(ValueError, match="default_area"):
            ts.validate_rec_registry_block(block, where="t")

    def test_two_areas_on_one_boundary_are_refused(self):
        """
        @verifies REQ-0002
        """
        block = boundary_manifest("rec-b", north="AC000E00001", upper="AC000E00001")["rec_registry"]
        with pytest.raises(ValueError, match="one boundary is one area"):
            ts.validate_rec_registry_block(block, where="t")

    @pytest.mark.parametrize(
        "key", ["north area", "-north", "_north", "north/south", "n" * 129, "zoné", ""]
    )
    def test_an_area_key_the_registry_cannot_take_is_refused(self, key):
        """Outside `^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`.

        @verifies REQ-0001
        """
        block = boundary_manifest("rec-b")["rec_registry"]
        block["areas"] = {key: {"boundary": {"source": SOURCE, "id": "AC000E00001"}}}
        with pytest.raises(ValueError, match="not a valid registry area key"):
            ts.validate_rec_registry_block(block, where="t")

    @pytest.mark.parametrize("key", ["north", "area_north", "valley-north-2", "N1", "7", "n" * 128])
    def test_a_readable_area_key_is_accepted(self, key):
        """
        @verifies REQ-0001
        """
        block = boundary_manifest("rec-b")["rec_registry"]
        block["areas"] = {key: {"boundary": {"source": SOURCE, "id": "AC000E00001"}}}
        ts.validate_rec_registry_block(block, where="t")

    @pytest.mark.parametrize("name", ["North valley", "Valle nord – cabina 1", "n" * 128])
    def test_an_area_may_carry_a_display_name(self, name):
        """
        @verifies REQ-0021
        """
        block = boundary_manifest("rec-b")["rec_registry"]
        block["areas"] = {
            "north": {"name": name, "boundary": {"source": SOURCE, "id": "AC000E00001"}}
        }
        ts.validate_rec_registry_block(block, where="t")

    @pytest.mark.parametrize(
        "name",
        ["", "   ", " North", "North ", "n" * 129, None, 7, ["North"], {"it": "Nord"}],
        ids=["empty", "blank", "leading", "trailing", "too-long", "null", "int", "list", "map"],
    )
    def test_a_display_name_that_is_not_one_is_refused(self, name):
        """
        @verifies REQ-0021
        """
        block = boundary_manifest("rec-b")["rec_registry"]
        block["areas"] = {
            "north": {"name": name, "boundary": {"source": SOURCE, "id": "AC000E00001"}}
        }
        with pytest.raises(ValueError, match=r"\.name must be the area's display name"):
            ts.validate_rec_registry_block(block, where="t")

    def test_the_display_name_defaults_to_the_key(self, seed_rec):
        """
        @verifies REQ-0021
        """
        manifest = boundary_manifest("rec-n", north="AC000E00001", south="AC000E00002")
        manifest["rec_registry"]["areas"]["north"]["name"] = "North valley"
        manifest.pop("slug")
        seed_rec("rec-n", **manifest)

        binding = ts.rec_registry_binding("rec-n")

        assert binding.area_name("north") == "North valley"
        assert binding.area_name("south") == "south"
        # The name is not the key: a member's area, and the boundary lookup, use the key.
        assert binding.area_for_boundary(SOURCE, "AC000E00001") == "north"

    def test_coverage_rules_beside_boundaries_are_refused(self):
        """
        @verifies REQ-0017
        """
        manifest = boundary_manifest("rec-b", north="AC000E00001")
        manifest["coverage"] = {"rules": [{"type": "municipality", "values": ["Springfield"]}]}
        with pytest.raises(ValueError, match="coverage"):
            ts.validate_boundary_template(manifest, where="t")

    def test_legacy_coverage_municipalities_beside_boundaries_are_refused(self):
        """
        @verifies REQ-0017
        """
        manifest = boundary_manifest("rec-b", north="AC000E00001")
        manifest["coverage"] = {"type": "municipalities", "municipalities": ["Springfield"]}
        with pytest.raises(ValueError, match="coverage"):
            ts.validate_boundary_template(manifest, where="t")

    @pytest.mark.parametrize(
        "steps", [["consents", "personal", "review"], None], ids=["listed", "default"]
    )
    def test_a_boundary_template_without_an_eligibility_step_is_refused(self, steps):
        """
        @verifies REQ-0017
        """
        manifest = boundary_manifest("rec-b", north="AC000E00001")
        if steps is None:
            del manifest["steps"]
        else:
            manifest["steps"] = steps
        with pytest.raises(ValueError, match="eligibility"):
            ts.validate_boundary_template(manifest, where="t")

    @pytest.mark.parametrize(
        "steps",
        [
            ["eligibility", "consents", "personal", "review"],
            ["personal", "eligibility", "consents", "review"],
            ["personal", "eligibility", "review"],
        ],
        ids=["first", "before-consents", "no-consents"],
    )
    def test_an_eligibility_step_before_the_submission_exists_is_refused(self, steps):
        """The checked address is saved on the submission `consents` creates.

        @verifies REQ-0017
        """
        manifest = boundary_manifest("rec-b", north="AC000E00001")
        manifest["steps"] = steps
        with pytest.raises(ValueError, match="after the 'consents' step"):
            ts.validate_boundary_template(manifest, where="t")

    @pytest.mark.parametrize(
        "steps",
        [
            ["consents", "eligibility", "personal", "review"],
            ["consents", "personal", {"custom": "energy"}, "eligibility", "review"],
        ],
        ids=["right-after", "later"],
    )
    def test_an_eligibility_step_after_consents_is_accepted(self, steps):
        """
        @verifies REQ-0017
        """
        manifest = boundary_manifest("rec-b", north="AC000E00001")
        manifest["steps"] = steps
        ts.validate_boundary_template(manifest, where="t")

    def test_a_municipality_template_is_untouched_by_the_boundary_checks(self):
        """
        @verifies REQ-0004
        """
        manifest = {
            "coverage": {"rules": [{"type": "municipality", "values": ["Springfield"]}]},
            "rec_registry": {
                "community": "example-rec",
                "default_area": "north",
                "areas": {"north": ["Springfield"]},
            },
        }
        ts.validate_rec_registry_block(manifest["rec_registry"], where="t")
        ts.validate_boundary_template(manifest, where="t")
        assert not ts.declares_boundaries(manifest["rec_registry"])


class TestTemplateVerification:
    async def test_known_boundaries_pass(self, fake_dt):
        """
        @verifies REQ-0002
        """
        manifest = boundary_manifest("rec-b", north="AC000E00001", south="AC000E00002")
        await boundaries.verify_template_boundaries(manifest, where="t")

        [(_, fetcher, payload, auth)] = fake_dt.calls()
        assert fetcher == "boundary_shape"
        assert payload == {"source": SOURCE, "ids": ["AC000E00001", "AC000E00002"]}
        assert auth == f"Bearer {SERVICE_TOKEN}"

    async def test_an_unknown_id_is_refused_and_named(self, fake_dt):
        """
        @verifies REQ-0002
        """
        manifest = boundary_manifest("rec-b", north="AC000E00001", south="AC000E00009")
        with pytest.raises(ValueError, match="AC000E00009"):
            await boundaries.verify_template_boundaries(manifest, where="t")

    async def test_an_unreachable_digital_twin_refuses_the_template(self, fake_dt):
        """
        @verifies REQ-0002
        """
        fake_dt.failure = httpx.ConnectError("refused")
        manifest = boundary_manifest("rec-b", north="AC000E00001")
        with pytest.raises(ValueError, match="not imported"):
            await boundaries.verify_template_boundaries(manifest, where="t")

    async def test_a_template_without_boundaries_asks_nothing(self, fake_dt):
        """
        @verifies REQ-0004
        """
        manifest = {"rec_registry": {"community": "c", "default_area": "north"}}
        await boundaries.verify_template_boundaries(manifest, where="t")
        assert fake_dt.calls() == []


class TestImportTemplates:
    """`onboarding-cli import-templates` refuses before anything is stored."""

    def _write(self, tmp_path, manifest):
        import yaml

        slug = manifest["slug"]
        (tmp_path / slug).mkdir()
        (tmp_path / slug / "manifest.yaml").write_text(yaml.safe_dump(manifest))

    def _run(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        from celine.onboarding.cli.main import app
        from celine.onboarding.models import database

        stored: list = []

        def _no_database():
            stored.append("opened")
            raise AssertionError("a refused import must not reach the database")

        monkeypatch.setattr(database, "async_session", _no_database)
        result = CliRunner().invoke(
            app, ["import-templates", "--all", "--templates-dir", str(tmp_path)]
        )
        return result, stored

    def test_an_unknown_boundary_refuses_the_import(self, tmp_path, monkeypatch, fake_dt):
        """
        @verifies REQ-0002
        """
        self._write(tmp_path, boundary_manifest("rec-b", north="AC000E00009"))
        result, stored = self._run(tmp_path, monkeypatch)

        assert result.exit_code == 1
        assert "AC000E00009" in result.output
        assert stored == []

    def test_an_unreachable_digital_twin_refuses_the_import(self, tmp_path, monkeypatch, fake_dt):
        """
        @verifies REQ-0002
        """
        fake_dt.failure = httpx.ConnectError("refused")
        self._write(tmp_path, boundary_manifest("rec-b", north="AC000E00001"))
        result, stored = self._run(tmp_path, monkeypatch)

        assert result.exit_code == 1
        assert "could not be checked" in result.output
        assert stored == []

    def test_coverage_rules_beside_boundaries_refuse_the_import(
        self, tmp_path, monkeypatch, fake_dt
    ):
        """
        @verifies REQ-0017
        """
        manifest = boundary_manifest("rec-b", north="AC000E00001")
        manifest["coverage"] = {"rules": [{"type": "municipality", "values": ["Springfield"]}]}
        self._write(tmp_path, manifest)
        result, stored = self._run(tmp_path, monkeypatch)

        assert result.exit_code == 1
        assert "coverage" in result.output
        assert stored == [] and fake_dt.calls() == []
