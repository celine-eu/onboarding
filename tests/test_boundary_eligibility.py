"""The anonymous eligibility check for a community whose areas are boundaries.

Through the real routes and the real SDK client, with `respx` as the Digital
Twin (`fake_digital_twin.py`, synthetic squares) and the geocoder stubbed to a
chosen point. What is asserted is what an anonymous caller can learn: yes or
no, never where; a Digital Twin that does not answer is a 503, never a verdict;
the route is rate-limited and a caller over the limit costs nothing.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from fake_digital_twin import (
    INSIDE_1,
    INSIDE_2,
    INSIDE_3,
    ON_SHARED_EDGE,
    OUTSIDE_ALL,
    boundary_manifest,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from celine.onboarding.api import deps
from celine.onboarding.config.settings import settings
from celine.onboarding.services import eligibility
from celine.onboarding.services.eligibility import AddressInfo


@pytest.fixture()
def geocoder(monkeypatch):
    """The geocoder, answering the point a test puts in `where`."""

    class _Geocoder:
        where: tuple[float, float] = INSIDE_1
        calls = 0

    state = _Geocoder()

    async def _geocode(address: str) -> AddressInfo:
        state.calls += 1
        lat, lng = state.where
        return AddressInfo(
            lat=lat,
            lng=lng,
            display_name="Via Example 1, Springfield",
            municipality="Springfield",
            municipality_candidates=("Springfield",),
            postal_code="00000",
            state="Example",
            country_code="IT",
        )

    async def _reverse(lat, lng):
        state.calls += 1
        return await _geocode("")

    monkeypatch.setattr(eligibility, "geocode_address", _geocode)
    monkeypatch.setattr(eligibility, "reverse_geocode", _reverse)
    import celine.onboarding.api.eligibility as api_eligibility
    import celine.onboarding.api.recs as api_recs

    monkeypatch.setattr(api_eligibility, "geocode_address", _geocode)
    monkeypatch.setattr(api_eligibility, "reverse_geocode", _reverse)
    monkeypatch.setattr(api_recs, "geocode_address", _geocode)
    return state


@pytest.fixture()
def client(seed_rec, monkeypatch):
    from celine.onboarding.api.eligibility import router as eligibility_router
    from celine.onboarding.api.recs import router as recs_router

    manifest = boundary_manifest("rec-b", north="AC000E00002", south="AC000E00003")
    manifest.pop("slug")
    seed_rec("rec-b", **manifest)

    monkeypatch.setattr(settings, "rate_limit_eligibility", "1000/minute")
    deps.limiter.reset()

    app = FastAPI()
    app.state.limiter = deps.limiter

    @app.exception_handler(RateLimitExceeded)
    async def _limited(request: Request, exc: RateLimitExceeded):
        return JSONResponse(status_code=429, content={"detail": "Too many requests."})

    app.include_router(eligibility_router, prefix="/api/{rec_slug}")
    app.include_router(recs_router, prefix="/api")
    yield TestClient(app)
    deps.limiter.reset()


def _check(client, rec="rec-b"):
    return client.post(f"/api/{rec}/eligibility", json={"address": "Via Example 1"})


class TestTheBoundaryDecides:
    def test_inside_a_declared_boundary_is_eligible(self, client, fake_dt, geocoder):
        """
        @verifies REQ-0003
        """
        geocoder.where = INSIDE_2
        response = _check(client)

        assert response.status_code == 200
        assert response.json()["eligible"] is True

    def test_inside_another_declared_boundary_is_eligible(self, client, fake_dt, geocoder):
        """
        @verifies REQ-0003
        """
        geocoder.where = INSIDE_3
        assert _check(client).json()["eligible"] is True

    def test_inside_a_boundary_the_template_does_not_declare_is_not_eligible(
        self, client, fake_dt, geocoder
    ):
        """
        @verifies REQ-0003
        """
        geocoder.where = INSIDE_1
        response = _check(client)

        assert response.status_code == 200
        assert response.json()["eligible"] is False

    def test_inside_no_boundary_at_all_is_not_eligible(self, client, fake_dt, geocoder):
        """
        @verifies REQ-0003
        """
        geocoder.where = OUTSIDE_ALL
        assert _check(client).json()["eligible"] is False

    def test_a_point_on_a_shared_edge_belongs_to_the_lowest_id(self, client, fake_dt, geocoder):
        """D28: the edge between AC000E00001 and AC000E00002 resolves to 00001,
        which this template does not declare — so not eligible, though 00002 is
        declared and touches the point.

        @verifies REQ-0003
        """
        geocoder.where = ON_SHARED_EDGE
        assert _check(client).json()["eligible"] is False

    def test_the_shared_edge_is_inside_when_the_lowest_id_is_declared(
        self, client, fake_dt, geocoder, seed_rec
    ):
        """
        @verifies REQ-0003
        """
        manifest = boundary_manifest("rec-e", east="AC000E00001", west="AC000E00002")
        manifest.pop("slug")
        seed_rec("rec-e", **manifest)
        geocoder.where = ON_SHARED_EDGE

        assert _check(client, "rec-e").json()["eligible"] is True

    def test_no_fallback_to_a_municipality(self, client, fake_dt, geocoder, seed_rec):
        """The geocoder names a municipality; for a boundary template it decides
        nothing, even if a stale manifest still lists it in `coverage`.

        @verifies REQ-0003
        """
        manifest = boundary_manifest("rec-s", north="AC000E00002")
        manifest.pop("slug")
        manifest["coverage"] = {"rules": [{"type": "municipality", "values": ["Springfield"]}]}
        seed_rec("rec-s", **manifest)
        geocoder.where = OUTSIDE_ALL

        assert _check(client, "rec-s").json()["eligible"] is False


class TestTheAnswerSaysNothingAboutWhere:
    @pytest.mark.parametrize("where", [INSIDE_2, OUTSIDE_ALL, INSIDE_1], ids=["in", "out", "other"])
    def test_no_boundary_id_no_area_and_no_matched_rule(self, client, fake_dt, geocoder, where):
        """
        @verifies REQ-0006
        """
        geocoder.where = where
        body = _check(client).json()
        text = str(body)

        assert body["matched_rule"] is None
        assert body["matched_value"] is None
        for leak in ("AC000E0000", "north", "south", "gse_cabine_primarie"):
            assert leak not in text

    @pytest.mark.parametrize("where", [INSIDE_2, OUTSIDE_ALL], ids=["in", "out"])
    def test_no_coordinate_for_an_address(self, client, fake_dt, geocoder, where):
        """The point geocoded from the caller's address is never handed back.

        @verifies REQ-0006
        """
        geocoder.where = where
        response = _check(client)

        assert response.status_code == 200
        body = response.json()
        for key in ("lat", "lng", "lon", "latitude", "longitude"):
            assert key not in body
        assert str(where[0]) not in response.text
        assert str(where[1]) not in response.text

    def test_no_coordinate_for_a_municipality_community_either(
        self, client, fake_dt, geocoder, seed_rec
    ):
        """
        @verifies REQ-0006
        """
        seed_rec("rec-m", coverage={"rules": [{"type": "municipality", "values": ["Springfield"]}]})
        response = _check(client, "rec-m")

        assert response.json()["eligible"] is True
        assert "lat" not in response.json() and "lng" not in response.json()
        assert str(INSIDE_1[0]) not in response.text

    def test_a_point_request_is_not_echoed(self, client, fake_dt, geocoder, seed_rec):
        """
        @verifies REQ-0006
        """
        seed_rec("rec-m", coverage={"rules": [{"type": "municipality", "values": ["Springfield"]}]})
        response = client.post("/api/rec-m/eligibility", json={"lat": 0.0473, "lng": 0.0519})

        assert response.status_code == 200
        assert "lat" not in response.json() and "lng" not in response.json()

    def test_the_sweep_carries_no_coordinate(self, client, fake_dt, geocoder, monkeypatch):
        """
        @verifies REQ-0019
        """
        from celine.onboarding.services import template_service

        monkeypatch.setattr(template_service, "get_slugs", lambda: ["rec-b"])
        geocoder.where = INSIDE_2
        response = client.post("/api/recs/find-by-address", json={"address": "x"})

        [found] = response.json()["matches"]
        for key in ("lat", "lng"):
            assert key not in response.json()
            assert key not in found
        assert str(INSIDE_2[1]) not in response.text

    def test_the_call_to_the_digital_twin_is_this_services_not_the_callers(
        self, client, fake_dt, geocoder
    ):
        """
        @verifies REQ-0006
        """
        client.post(
            "/api/rec-b/eligibility",
            json={"address": "Via Example 1"},
            headers={"Authorization": "Bearer somebody-elses-token"},
        )
        [(_, _, _, auth)] = fake_dt.calls()
        assert auth == "Bearer svc-onboarding-token"


class TestItFailsClosed:
    @pytest.mark.parametrize(
        "failure",
        [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), 401, 500],
        ids=["unreachable", "timeout", "token-refused", "error"],
    )
    def test_a_digital_twin_that_does_not_answer_is_503_never_a_verdict(
        self, client, fake_dt, geocoder, failure
    ):
        """
        @verifies REQ-0005
        """
        fake_dt.failure = failure
        response = _check(client)

        assert response.status_code == 503
        assert "cannot check your address right now" in response.json()["detail"]
        assert "eligible" not in response.json()

    def test_nothing_about_the_address_is_logged(self, client, fake_dt, geocoder, caplog):
        """
        @verifies REQ-0005
        """
        caplog.set_level(logging.DEBUG)
        fake_dt.failure = 500
        geocoder.where = (0.054321, 0.154321)
        _check(client)

        assert "0.054321" not in caplog.text
        assert "0.154321" not in caplog.text
        assert "Via Example" not in caplog.text


class TestATemplateWithoutBoundaries:
    def test_keeps_the_municipality_path_and_asks_no_digital_twin(
        self, client, fake_dt, geocoder, seed_rec
    ):
        """
        @verifies REQ-0004
        """
        seed_rec(
            "rec-m",
            coverage={"rules": [{"type": "municipality", "values": ["Springfield"]}]},
            rec_registry={"community": "c", "default_area": "north", "areas": {"north": ["X"]}},
        )
        body = _check(client, "rec-m").json()

        assert body["eligible"] is True
        assert body["matched_rule"] == "municipality"
        assert body["matched_value"] == "Springfield"
        assert fake_dt.calls() == []

    def test_a_municipality_outside_the_rules_is_refused_as_before(
        self, client, fake_dt, geocoder, seed_rec
    ):
        """
        @verifies REQ-0004
        """
        seed_rec("rec-m", coverage={"rules": [{"type": "municipality", "values": ["Shelbyville"]}]})
        assert _check(client, "rec-m").json()["eligible"] is False
        assert fake_dt.calls() == []


class TestTheRateLimit:
    def test_over_the_limit_is_429_and_costs_nothing(self, client, fake_dt, geocoder, monkeypatch):
        """
        @verifies REQ-0016
        """
        monkeypatch.setattr(settings, "rate_limit_eligibility", "2/minute")
        deps.limiter.reset()
        geocoder.where = INSIDE_2

        assert _check(client).status_code == 200
        assert _check(client).status_code == 200
        geocoder_calls, dt_calls = geocoder.calls, len(fake_dt.calls())

        response = _check(client)

        assert response.status_code == 429
        assert geocoder.calls == geocoder_calls
        assert len(fake_dt.calls()) == dt_calls

    def test_the_limit_is_per_client(self, client, fake_dt, geocoder, monkeypatch):
        """Keyed on the caller's address, as the other public routes are: one
        caller over the limit leaves another's budget untouched.

        @verifies REQ-0016
        """
        monkeypatch.setattr(settings, "rate_limit_eligibility", "1/minute")
        deps.limiter.reset()
        first = TestClient(client.app, client=("198.51.100.1", 50000))
        second = TestClient(client.app, client=("198.51.100.2", 50000))

        assert _check(first).status_code == 200
        assert _check(first).status_code == 429
        assert _check(second).status_code == 200

    def test_the_limit_is_configurable(self, client, fake_dt, geocoder, monkeypatch):
        """
        @verifies REQ-0016
        """
        monkeypatch.setattr(settings, "rate_limit_eligibility", "1/minute")
        deps.limiter.reset()
        assert _check(client).status_code == 200
        assert _check(client).status_code == 429

    def test_the_sweep_across_communities_is_limited_too(
        self, client, fake_dt, geocoder, monkeypatch
    ):
        """
        @verifies REQ-0019
        """
        monkeypatch.setattr(settings, "rate_limit_eligibility", "1/minute")
        deps.limiter.reset()
        assert client.post("/api/recs/find-by-address", json={"address": "x"}).status_code == 200
        assert client.post("/api/recs/find-by-address", json={"address": "x"}).status_code == 429


class TestTheSweep:
    def test_a_boundary_community_is_found_by_boundary_and_names_nothing(
        self, client, fake_dt, geocoder, monkeypatch
    ):
        """
        @verifies REQ-0006
        """
        from celine.onboarding.services import template_service

        monkeypatch.setattr(template_service, "get_slugs", lambda: ["rec-b"])
        geocoder.where = INSIDE_2
        [found] = client.post("/api/recs/find-by-address", json={"address": "x"}).json()["matches"]

        assert found["slug"] == "rec-b"
        assert found["matched_rule"] is None and found["matched_value"] is None

    def test_a_boundary_community_outside_is_not_listed(
        self, client, fake_dt, geocoder, monkeypatch
    ):
        from celine.onboarding.services import template_service

        monkeypatch.setattr(template_service, "get_slugs", lambda: ["rec-b"])
        geocoder.where = OUTSIDE_ALL
        assert client.post("/api/recs/find-by-address", json={"address": "x"}).json() == {
            "matches": [],
            "unchecked": False,
        }

    @pytest.fixture()
    def with_municipality_community(self, seed_rec, monkeypatch):
        """Beside rec-b, rec-m: a municipality community covering Springfield."""
        from celine.onboarding.services import template_service

        seed_rec(
            "rec-m",
            coverage={"rules": [{"type": "municipality", "values": ["Springfield"]}]},
        )
        monkeypatch.setattr(template_service, "get_slugs", lambda: ["rec-b", "rec-m"])

    @pytest.mark.parametrize(
        "failure", [httpx.ConnectError("refused"), 503], ids=["unreachable", "refusing"]
    )
    def test_an_outage_leaves_out_only_the_boundary_community(
        self, client, fake_dt, geocoder, with_municipality_community, failure
    ):
        """The boundary community fails closed on its own; the municipality
        community is answered as it would be without a Digital Twin.

        @verifies REQ-0019
        @verifies REQ-0005
        """
        geocoder.where = INSIDE_2
        fake_dt.failure = failure
        response = client.post("/api/recs/find-by-address", json={"address": "x"})

        assert response.status_code == 200
        assert [c["slug"] for c in response.json()["matches"]] == ["rec-m"]
        # Said, so the page can ask to try again rather than claim no match.
        assert response.json()["unchecked"] is True

    def test_with_the_digital_twin_up_both_are_listed(
        self, client, fake_dt, geocoder, with_municipality_community
    ):
        """
        @verifies REQ-0019
        """
        geocoder.where = INSIDE_2
        response = client.post("/api/recs/find-by-address", json={"address": "x"})
        assert sorted(c["slug"] for c in response.json()["matches"]) == ["rec-b", "rec-m"]
        assert response.json()["unchecked"] is False

    def test_an_outage_with_only_boundary_communities_admits_nobody(
        self, client, fake_dt, geocoder, monkeypatch
    ):
        """
        @verifies REQ-0019
        """
        from celine.onboarding.services import template_service

        monkeypatch.setattr(template_service, "get_slugs", lambda: ["rec-b"])
        geocoder.where = INSIDE_2
        fake_dt.failure = httpx.ConnectError("refused")
        response = client.post("/api/recs/find-by-address", json={"address": "x"})
        assert response.status_code == 200
        assert response.json() == {"matches": [], "unchecked": True}

    def test_the_outage_log_names_the_community_not_the_point(
        self, client, fake_dt, geocoder, with_municipality_community, caplog
    ):
        """
        @verifies REQ-0019
        """
        caplog.set_level(logging.DEBUG)
        geocoder.where = (0.054321, 0.154321)
        fake_dt.failure = 500
        client.post("/api/recs/find-by-address", json={"address": "Via Example 7"})

        assert "rec-b" in caplog.text
        assert "0.054321" not in caplog.text and "0.154321" not in caplog.text
        assert "Via Example 7" not in caplog.text
