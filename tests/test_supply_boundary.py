"""What a submission keeps about where its supply point is, and who decides it.

For a community whose areas are boundaries the service resolves the boundary
from the submission's own supply address — on save, on submit and again at
approval — and records its id and source, never the coordinates and never a
value a client sent. Approval maps it to an area of the template in force then,
and refuses with `boundary_not_in_community` rather than falling back.

`respx` is the Digital Twin (`fake_digital_twin.py`, synthetic squares); the
geocoder is stubbed to a point per address.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fake_digital_twin import (
    INSIDE_1,
    INSIDE_2,
    INSIDE_3,
    OUTSIDE_ALL,
    boundary_manifest,
)

from celine.onboarding.models.schemas import (
    SubmissionAdminRead,
    SubmissionRead,
    SubmissionUpdate,
)
from celine.onboarding.models.submission import Submission, SubmissionStatus
from celine.onboarding.services import (
    eligibility,
    rec_registry,
    submission_service,
    supply_boundary,
)
from celine.onboarding.services.boundaries import (
    BoundaryNotInCommunityError,
    BoundaryUnavailableError,
)
from celine.onboarding.services.eligibility import AddressInfo

#: Synthetic addresses, and the point the stubbed geocoder puts each at.
ADDRESSES = {
    "Via Example 2": INSIDE_2,  # AC000E00002, area north
    "Via Example 3": INSIDE_3,  # AC000E00003, area south
    "Via Example 1": INSIDE_1,  # AC000E00001, no area here
    "Via Nowhere 9": OUTSIDE_ALL,  # no boundary
}


@pytest.fixture()
def geocoder(monkeypatch):
    calls: list[str] = []

    async def _geocode(address: str) -> AddressInfo:
        calls.append(address)
        if address not in ADDRESSES:
            raise ValueError(f"Address not found: {address}")
        lat, lng = ADDRESSES[address]
        return AddressInfo(lat=lat, lng=lng, display_name=address)

    monkeypatch.setattr(eligibility, "geocode_address", _geocode)
    return calls


@pytest.fixture()
def rec(seed_rec):
    """A boundary community: north is AC000E00002, south is AC000E00003."""

    def _seed(**areas):
        manifest = boundary_manifest(
            "rec-b", **(areas or {"north": "AC000E00002", "south": "AC000E00003"})
        )
        manifest.pop("slug")
        manifest["rec_registry"]["community"] = "example-rec"
        return seed_rec("rec-b", **manifest)

    _seed()
    return _seed


def _submission(address: str | None = "Via Example 2", **fields) -> Submission:
    sub = Submission(
        id=uuid.uuid4(),
        ref="20260927-abcd1234",
        rec_slug="rec-b",
        status=SubmissionStatus.DRAFT,
        first_name="Ada",
        last_name="Example",
        email="ada@example.org",
        fiscal_code="XXXXXX00X00X000X",
        pod_code="IT001E00000001",
        gdpr_consent=True,
        policy_consent=True,
        statute_consent=True,
        consent_ip="198.51.100.7",
        extracted_data={"indirizzo": address} if address else None,
        extra_data={},
        data_sharing_consent=False,
        share_provisioned=False,
        keep_me_updated=False,
        phone_verified=False,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        **fields,
    )
    return sub


class FakeDb:
    def __init__(self) -> None:
        self.commits = 0
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj) -> None:
        pass


@pytest.fixture()
def db(monkeypatch):
    fake = FakeDb()

    async def _get(db, submission_id):
        return _get.current

    _get.current = None
    monkeypatch.setattr(submission_service, "get_submission", _get)
    fake.current = _get
    return fake


async def _save(db, sub, **fields):
    db.current.current = sub
    return await submission_service.update_submission(db, sub, SubmissionUpdate(**fields))


# ── what a client can send, and what it can read ─────────────────────────────


class TestNoClientDecidesTheBoundary:
    def test_no_request_schema_declares_it(self):
        """
        @verifies REQ-0007
        """
        from celine.onboarding.models.schemas import ConsentCreate

        for schema in (SubmissionUpdate, ConsentCreate):
            assert "supply_boundary_id" not in schema.model_fields
            assert "supply_boundary_source" not in schema.model_fields

    def test_a_value_a_client_sends_is_dropped(self):
        """
        @verifies REQ-0007
        """
        update = SubmissionUpdate(supply_boundary_id="AC000E00003", supply_boundary_source="x")
        assert update.model_dump(exclude_unset=True) == {}

    async def test_a_value_a_client_sends_is_never_stored(self, rec, db, fake_dt, geocoder):
        """The client names 00003; the address resolves to 00002, and 00002 is kept.

        @verifies REQ-0007
        """
        sub = _submission(address=None)
        update = SubmissionUpdate.model_validate(
            {
                "extracted_data": {"indirizzo": "Via Example 2"},
                "supply_boundary_id": "AC000E00003",
                "supply_boundary_source": "gse_cabine_primarie",
            }
        )
        db.current.current = sub
        await submission_service.update_submission(db, sub, update)

        assert sub.supply_boundary_id == "AC000E00002"

    def test_the_applicant_is_not_told_where(self):
        """The wizard's own read carries neither field (ADR-0013).

        @verifies REQ-0006
        """
        assert "supply_boundary_id" not in SubmissionRead.model_fields
        assert "supply_boundary_source" not in SubmissionRead.model_fields

    def test_the_reviewing_operator_is(self):
        """
        @verifies REQ-0007
        """
        for field in ("supply_boundary_id", "supply_boundary_source", "supply_boundary_area"):
            assert field in SubmissionAdminRead.model_fields


# ── on save ──────────────────────────────────────────────────────────────────


class TestOnSave:
    async def test_the_boundary_is_resolved_and_recorded_with_its_source(
        self, rec, db, fake_dt, geocoder
    ):
        """
        @verifies REQ-0007
        """
        sub = _submission(address=None)
        await _save(db, sub, extracted_data={"indirizzo": "Via Example 3"})

        assert sub.supply_boundary_id == "AC000E00003"
        assert sub.supply_boundary_source == "gse_cabine_primarie"
        assert db.commits == 1

    async def test_no_coordinates_are_stored(self, rec, db, fake_dt, geocoder):
        """Nothing on the submission holds the point: no column for it, and no
        stored value carries either number.

        @verifies REQ-0007
        """
        sub = _submission(address=None)
        await _save(db, sub, extracted_data={"indirizzo": "Via Example 3"})

        columns = {c.name for c in Submission.__table__.columns}
        assert not {
            c for c in columns if any(w in c for w in ("lat", "lon", "lng", "coord", "point"))
        }

        lat, lon = INSIDE_3
        stored = json.dumps({c: getattr(sub, c) for c in columns}, default=str)
        assert str(lat) not in stored
        assert str(lon) not in stored

    async def test_a_changed_address_resolves_again(self, rec, db, fake_dt, geocoder):
        """
        @verifies REQ-0007
        """
        sub = _submission(
            "Via Example 2",
            supply_boundary_id="AC000E00002",
            supply_boundary_source="gse_cabine_primarie",
        )
        await _save(db, sub, extracted_data={"indirizzo": "Via Example 3"})

        assert sub.supply_boundary_id == "AC000E00003"

    async def test_an_unchanged_address_is_not_asked_again(self, rec, db, fake_dt, geocoder):
        sub = _submission(
            "Via Example 2",
            supply_boundary_id="AC000E00002",
            supply_boundary_source="gse_cabine_primarie",
        )
        await _save(db, sub, locale="en")

        assert geocoder == [] and fake_dt.calls() == []
        assert sub.supply_boundary_id == "AC000E00002"

    async def test_an_unanswered_check_records_nothing_and_keeps_the_save(
        self, rec, db, fake_dt, geocoder
    ):
        """
        @verifies REQ-0005
        """
        fake_dt.failure = httpx.ConnectError("refused")
        sub = _submission(address=None)
        await _save(db, sub, extracted_data={"indirizzo": "Via Example 3"})

        assert sub.supply_boundary_id is None
        assert sub.supply_boundary_source is None
        assert db.commits == 1

    async def test_a_changed_address_that_cannot_be_checked_forgets_the_old_id(
        self, rec, db, fake_dt, geocoder
    ):
        """The stored id is always what the current address resolves to, or nothing.

        @verifies REQ-0007
        """
        fake_dt.failure = 503
        sub = _submission(
            "Via Example 2",
            supply_boundary_id="AC000E00002",
            supply_boundary_source="gse_cabine_primarie",
        )
        await _save(db, sub, extracted_data={"indirizzo": "Via Example 3"})

        assert sub.supply_boundary_id is None

    async def test_outside_every_boundary_records_no_id(self, rec, db, fake_dt, geocoder):
        sub = _submission(address=None)
        await _save(db, sub, extracted_data={"indirizzo": "Via Nowhere 9"})
        assert sub.supply_boundary_id is None and sub.supply_boundary_source is None

    async def test_a_template_without_boundaries_resolves_nothing(
        self, seed_rec, db, fake_dt, geocoder
    ):
        """
        @verifies REQ-0004
        """
        seed_rec("rec-b", rec_registry={"community": "c", "default_area": "north"})
        sub = _submission(address=None)
        await _save(db, sub, extracted_data={"indirizzo": "Via Example 3"})

        assert sub.supply_boundary_id is None
        assert geocoder == [] and fake_dt.calls() == []

    async def test_the_address_is_not_logged(self, rec, db, fake_dt, geocoder, caplog):
        caplog.set_level(logging.DEBUG)
        fake_dt.failure = 500
        await _save(db, _submission(address=None), extracted_data={"indirizzo": "Via Example 3"})
        assert "Via Example 3" not in caplog.text


# ── on submit ────────────────────────────────────────────────────────────────


class TestOnSubmit:
    async def test_inside_a_declared_boundary_submits_and_records_it(
        self, rec, db, fake_dt, geocoder
    ):
        """
        @verifies REQ-0007
        """
        sub = _submission("Via Example 2")
        await _save(db, sub, status=SubmissionStatus.SUBMITTED)

        assert sub.status == SubmissionStatus.SUBMITTED
        assert sub.supply_boundary_id == "AC000E00002"

    @pytest.mark.parametrize(
        "address", ["Via Example 1", "Via Nowhere 9"], ids=["undeclared", "no-boundary"]
    )
    async def test_outside_every_declared_boundary_cannot_be_submitted(
        self, rec, db, fake_dt, geocoder, address
    ):
        """
        @verifies REQ-0007
        """
        sub = _submission(address)
        with pytest.raises(ValueError, match="not in the community's area"):
            await _save(db, sub, status=SubmissionStatus.SUBMITTED)

        assert sub.status == SubmissionStatus.DRAFT
        assert db.commits == 0

    async def test_no_supply_address_cannot_be_submitted(self, rec, db, fake_dt, geocoder):
        """
        @verifies REQ-0007
        """
        sub = _submission(address=None)
        with pytest.raises(ValueError, match="no supply address"):
            await _save(db, sub, status=SubmissionStatus.SUBMITTED)
        assert db.commits == 0

    async def test_an_unanswered_check_refuses_the_submit_and_writes_nothing(
        self, rec, db, fake_dt, geocoder
    ):
        """
        @verifies REQ-0005
        """
        fake_dt.failure = httpx.ReadTimeout("slow")
        sub = _submission("Via Example 2")
        with pytest.raises(BoundaryUnavailableError):
            await _save(db, sub, status=SubmissionStatus.SUBMITTED)

        assert sub.status == SubmissionStatus.DRAFT
        assert sub.supply_boundary_id is None
        assert db.commits == 0

    async def test_a_template_without_boundaries_submits_as_before(
        self, seed_rec, db, fake_dt, geocoder
    ):
        """
        @verifies REQ-0004
        """
        seed_rec("rec-b", rec_registry={"community": "c", "default_area": "north"})
        sub = _submission(address=None)
        await _save(db, sub, status=SubmissionStatus.SUBMITTED)

        assert sub.status == SubmissionStatus.SUBMITTED
        assert fake_dt.calls() == []


class TestTheWizardRoute:
    """The status codes the wizard sees for a submit it cannot make."""

    @pytest.fixture()
    def client(self, rec, db, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from celine.onboarding.api import submissions as api
        from celine.onboarding.models.database import get_db

        sub = _submission("Via Example 2")
        db.current.current = sub

        async def _live(submission_id, request, *, rec_slug=None, db=None):
            return sub

        async def _db():
            yield db

        monkeypatch.setattr(api, "_get_live_submission", _live)
        app = FastAPI()
        app.include_router(api.router, prefix="/api/{rec_slug}")
        app.dependency_overrides[get_db] = _db
        return TestClient(app)

    def test_an_unanswered_check_is_503(self, client, fake_dt, geocoder):
        """
        @verifies REQ-0005
        """
        fake_dt.failure = httpx.ConnectError("refused")
        response = client.patch(
            f"/api/rec-b/submissions/{uuid.uuid4()}", json={"status": "submitted"}
        )

        assert response.status_code == 503
        assert "cannot check your address right now" in response.json()["detail"]

    def test_outside_the_community_is_422(self, client, fake_dt, geocoder, db):
        """
        @verifies REQ-0007
        """
        db.current.current.extracted_data = {"indirizzo": "Via Nowhere 9"}
        response = client.patch(
            f"/api/rec-b/submissions/{uuid.uuid4()}", json={"status": "submitted"}
        )
        assert response.status_code == 422

    def test_the_answer_does_not_carry_the_boundary(self, client, fake_dt, geocoder):
        """
        @verifies REQ-0006
        """
        response = client.patch(f"/api/rec-b/submissions/{uuid.uuid4()}", json={"locale": "en"})
        assert response.status_code == 200
        assert "AC000E0000" not in response.text


class TestTheReviewingOperatorSeesTheSubstation:
    def test_the_area_is_read_against_the_template_in_force(self, rec):
        """
        @verifies REQ-0007
        """
        sub = _submission(
            supply_boundary_id="AC000E00003", supply_boundary_source="gse_cabine_primarie"
        )
        assert supply_boundary.area_of(sub) == "south"

    def test_a_boundary_the_template_dropped_shows_no_area(self, rec):
        rec(north="AC000E00002")
        sub = _submission(
            supply_boundary_id="AC000E00003", supply_boundary_source="gse_cabine_primarie"
        )
        assert supply_boundary.area_of(sub) is None

    def test_the_admin_read_carries_id_source_and_area(self, rec):
        """
        @verifies REQ-0007
        """
        from celine.onboarding.api.admin.submissions import _read

        sub = _submission(
            supply_boundary_id="AC000E00002", supply_boundary_source="gse_cabine_primarie"
        )
        read = _read(sub)

        assert read.supply_boundary_id == "AC000E00002"
        assert read.supply_boundary_source == "gse_cabine_primarie"
        assert read.supply_boundary_area == "north"

    def test_the_review_names_the_area_by_its_display_name(self, seed_rec):
        """Primary substation <id>, area <name>: the template's name, not the key.

        @verifies REQ-0023
        """
        from celine.onboarding.api.admin.submissions import _read

        manifest = boundary_manifest("rec-b", north="AC000E00002", south="AC000E00003")
        manifest.pop("slug")
        manifest["rec_registry"]["areas"]["north"]["name"] = "North valley"
        seed_rec("rec-b", **manifest)
        sub = _submission(
            supply_boundary_id="AC000E00002", supply_boundary_source="gse_cabine_primarie"
        )
        read = _read(sub)

        assert read.supply_boundary_area == "north"
        assert read.supply_boundary_area_name == "North valley"

    def test_an_area_without_a_name_is_named_by_its_key(self, rec):
        """
        @verifies REQ-0023
        """
        from celine.onboarding.api.admin.submissions import _read

        sub = _submission(
            supply_boundary_id="AC000E00003", supply_boundary_source="gse_cabine_primarie"
        )
        assert _read(sub).supply_boundary_area_name == "south"

    def test_a_dropped_boundary_has_no_area_name(self, rec):
        """
        @verifies REQ-0023
        """
        from celine.onboarding.api.admin.submissions import _read

        rec(north="AC000E00002")
        sub = _submission(
            supply_boundary_id="AC000E00003", supply_boundary_source="gse_cabine_primarie"
        )
        read = _read(sub)
        assert read.supply_boundary_area is None
        assert read.supply_boundary_area_name is None

    def test_the_applicant_read_carries_no_area_name(self):
        """
        @verifies REQ-0023
        """
        assert "supply_boundary_area_name" not in SubmissionRead.model_fields


# ── at approval ──────────────────────────────────────────────────────────────


@pytest.fixture()
def registry(monkeypatch):
    calls: list = []

    class _Client:
        async def create_member(self, community, body):
            calls.append((community, body.to_dict()))
            return SimpleNamespace(status_code=201, content=b"")

    monkeypatch.setattr(rec_registry.settings, "rec_registry_url", "http://registry.test")
    monkeypatch.setattr(rec_registry, "_get_client", lambda: _Client())
    return calls


class TestAtApproval:
    async def test_the_member_is_registered_into_the_boundarys_area(
        self, rec, registry, fake_dt, geocoder
    ):
        """
        @verifies REQ-0003
        @verifies REQ-0008
        """
        sub = _submission("Via Example 3")
        assert await rec_registry.register_member(sub) == sub.ref

        [(community, body)] = registry
        assert community == "example-rec"
        assert body["area"] == "south"
        assert sub.supply_boundary_id == "AC000E00003"

    async def test_it_resolves_again_rather_than_trusting_the_stored_id(
        self, rec, registry, fake_dt, geocoder
    ):
        """
        @verifies REQ-0008
        """
        sub = _submission(
            "Via Example 3",
            supply_boundary_id="AC000E00002",
            supply_boundary_source="gse_cabine_primarie",
        )
        await rec_registry.register_member(sub)

        assert registry[0][1]["area"] == "south"
        assert sub.supply_boundary_id == "AC000E00003"
        assert fake_dt.calls("boundary_at_point")

    async def test_a_boundary_no_longer_in_the_template_is_refused_and_nothing_registered(
        self, rec, registry, fake_dt, geocoder
    ):
        """South was dropped from the template after the wizard ran.

        @verifies REQ-0008
        """
        sub = _submission(
            "Via Example 3",
            supply_boundary_id="AC000E00003",
            supply_boundary_source="gse_cabine_primarie",
        )
        rec(north="AC000E00002")

        with pytest.raises(
            BoundaryNotInCommunityError, match="boundary_not_in_community"
        ) as raised:
            await rec_registry.register_member(sub)

        assert raised.value.code == "boundary_not_in_community"
        assert registry == []

    async def test_no_fallback_to_the_stored_id_or_a_default(
        self, rec, registry, fake_dt, geocoder
    ):
        """The stored id is declared; the address now resolves outside. Refused.

        @verifies REQ-0008
        """
        sub = _submission(
            "Via Nowhere 9",
            supply_boundary_id="AC000E00002",
            supply_boundary_source="gse_cabine_primarie",
        )
        with pytest.raises(BoundaryNotInCommunityError):
            await rec_registry.register_member(sub)
        assert registry == []
        assert sub.supply_boundary_id is None

    async def test_an_unanswered_check_fails_and_registers_nothing(
        self, rec, registry, fake_dt, geocoder
    ):
        """
        @verifies REQ-0008
        @verifies REQ-0005
        """
        fake_dt.failure = httpx.ConnectError("refused")
        sub = _submission(
            "Via Example 3",
            supply_boundary_id="AC000E00003",
            supply_boundary_source="gse_cabine_primarie",
        )
        with pytest.raises(BoundaryUnavailableError):
            await rec_registry.register_member(sub)

        assert registry == []
        assert sub.supply_boundary_id == "AC000E00003"

    async def test_a_template_without_boundaries_keeps_the_municipality_area(
        self, seed_rec, registry, fake_dt, geocoder
    ):
        """
        @verifies REQ-0004
        """
        seed_rec(
            "rec-b",
            rec_registry={
                "community": "c",
                "default_area": "north",
                "areas": {"west": ["Springfield"]},
            },
        )
        sub = _submission("Via Example 3", supply_municipality="Springfield")
        await rec_registry.register_member(sub)

        assert registry[0][1]["area"] == "west"
        assert fake_dt.calls() == [] and geocoder == []

    def test_the_payload_never_falls_back_for_a_boundary_template(self, rec):
        """
        @verifies REQ-0008
        """
        from celine.onboarding.services import template_service

        binding = template_service.rec_registry_binding("rec-b")
        with pytest.raises(ValueError, match="no area was resolved"):
            rec_registry.build_member_payload(_submission(), binding)


class TestTheRegistryStepAndItsRetry:
    """Step 2 of the enablement pipeline: fails with the code, retries by resolving again."""

    async def test_the_step_fails_with_the_code_and_a_retry_succeeds_once_declared(
        self, rec, registry, fake_dt, geocoder
    ):
        """
        @verifies REQ-0008
        """
        from test_enablement import FakeDb as EnablementDb

        from celine.onboarding.models.enablement import EnablementStatus, EnablementStep
        from celine.onboarding.services import enablement

        rec(north="AC000E00002")
        db = EnablementDb()
        sub = _submission("Via Example 3")
        step = EnablementStep.REC_REGISTRY_MEMBER

        rows = await enablement.retry(db, sub, step=step)
        assert rows[step].status == EnablementStatus.FAILED
        assert "boundary_not_in_community" in rows[step].last_error
        assert registry == []

        rec(north="AC000E00002", south="AC000E00003")
        rows = await enablement.retry(db, sub, step=step)

        assert rows[step].status == EnablementStatus.SUCCEEDED
        assert registry[0][1]["area"] == "south"

    async def test_an_unanswered_check_fails_the_step_retryably(
        self, rec, registry, fake_dt, geocoder
    ):
        """
        @verifies REQ-0008
        """
        from test_enablement import FakeDb as EnablementDb

        from celine.onboarding.models.enablement import EnablementStatus, EnablementStep
        from celine.onboarding.services import enablement

        db = EnablementDb()
        sub = _submission("Via Example 2")
        step = EnablementStep.REC_REGISTRY_MEMBER
        fake_dt.failure = 503

        rows = await enablement.retry(db, sub, step=step)
        assert rows[step].status == EnablementStatus.FAILED
        assert "BoundaryUnavailableError" in rows[step].last_error

        fake_dt.failure = None
        rows = await enablement.retry(db, sub, step=step)
        assert rows[step].status == EnablementStatus.SUCCEEDED
        assert registry[0][1]["area"] == "north"


class TestTheMigration:
    def test_upgrade_adds_both_columns(self, monkeypatch):
        """
        @verifies REQ-0007
        """
        from test_sharing_intent import _offline_sql

        sql = _offline_sql(monkeypatch, "0014:0015")
        assert "ALTER TABLE submissions ADD COLUMN supply_boundary_id TEXT" in sql
        assert "ALTER TABLE submissions ADD COLUMN supply_boundary_source VARCHAR(40)" in sql

    def test_downgrade_drops_them(self, monkeypatch):
        from test_sharing_intent import _offline_sql

        sql = _offline_sql(monkeypatch, "0015:0014", downgrade=True)
        assert "DROP COLUMN supply_boundary_id" in sql
        assert "DROP COLUMN supply_boundary_source" in sql

    def test_it_is_the_head(self):
        from pathlib import Path

        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
        assert ScriptDirectory.from_config(config).get_heads() == ["0021"]

    def test_the_model_matches(self):
        columns = {c.name: c for c in Submission.__table__.columns}
        assert "supply_boundary_id" in columns and "supply_boundary_source" in columns
        assert columns["supply_boundary_source"].type.length == 40


# ── the address the wizard checked (D48) ─────────────────────────────────────


class TestTheCheckedSupplyAddress:
    """The wizard saves the address its eligibility step checked; the server
    resolves the boundary from it, before the scanned `indirizzo`."""

    def test_the_update_schema_takes_it_in_the_geocoders_shape(self):
        """
        @verifies REQ-0018
        """
        update = SubmissionUpdate(supply_address={"text": "  Via Example 2  "})
        assert update.model_dump(exclude_unset=True) == {
            "supply_address": {"text": "Via Example 2"}
        }

    @pytest.mark.parametrize(
        "value",
        [{"text": ""}, {"text": "x" * 301}, {"text": "Via Example 2", "lat": 0.0473}, {}],
        ids=["empty", "too-long", "a-point-beside-it", "no-text"],
    )
    def test_anything_else_is_refused(self, value):
        """Nothing a browser computed from the address rides along with it.

        @verifies REQ-0018
        """
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SubmissionUpdate(supply_address=value)

    def test_it_is_stored_encrypted(self):
        """
        @verifies REQ-0018
        """
        from celine.onboarding.models.encrypted import EncryptedJSON

        column = Submission.__table__.columns["supply_address"]
        assert isinstance(column.type, EncryptedJSON)

    def test_the_applicant_reads_back_their_own_address_not_the_boundary(self):
        """
        @verifies REQ-0018
        """
        assert "supply_address" in SubmissionRead.model_fields
        assert "supply_boundary_id" not in SubmissionRead.model_fields

    async def test_a_save_resolves_the_boundary_from_it(self, rec, db, fake_dt, geocoder):
        """
        @verifies REQ-0018
        @verifies REQ-0007
        """
        sub = _submission(address=None)
        await _save(db, sub, supply_address={"text": "Via Example 3"})

        assert sub.supply_address == {"text": "Via Example 3"}
        assert sub.supply_boundary_id == "AC000E00003"
        assert geocoder == ["Via Example 3"]

    async def test_it_wins_over_the_scanned_address(self, rec, db, fake_dt, geocoder):
        """The bill says south; the address the applicant checked says north.

        @verifies REQ-0018
        """
        sub = _submission("Via Example 3")
        await _save(db, sub, supply_address={"text": "Via Example 2"})
        assert sub.supply_boundary_id == "AC000E00002"

        await _save(db, sub, status=SubmissionStatus.SUBMITTED)
        assert sub.status == SubmissionStatus.SUBMITTED
        assert sub.supply_boundary_id == "AC000E00002"

    async def test_without_it_the_scanned_address_is_the_fallback(self, rec, db, fake_dt, geocoder):
        """
        @verifies REQ-0018
        """
        sub = _submission("Via Example 3")
        await _save(db, sub, status=SubmissionStatus.SUBMITTED)
        assert sub.supply_boundary_id == "AC000E00003"

    async def test_approval_resolves_from_it(self, rec, registry, fake_dt, geocoder):
        """
        @verifies REQ-0018
        @verifies REQ-0008
        """
        sub = _submission("Via Example 2", supply_address={"text": "Via Example 3"})
        await rec_registry.register_member(sub)

        assert registry[0][1]["area"] == "south"
        assert sub.supply_boundary_id == "AC000E00003"

    async def test_a_checked_address_outside_cannot_be_submitted(self, rec, db, fake_dt, geocoder):
        """
        @verifies REQ-0018
        """
        sub = _submission(address=None, supply_address={"text": "Via Nowhere 9"})
        with pytest.raises(ValueError, match="not in the community's area"):
            await _save(db, sub, status=SubmissionStatus.SUBMITTED)
        assert sub.status == SubmissionStatus.DRAFT

    async def test_it_is_not_logged(self, rec, db, fake_dt, geocoder, caplog):
        """
        @verifies REQ-0018
        """
        caplog.set_level(logging.DEBUG)
        fake_dt.failure = 500
        sub = _submission(address=None)
        await _save(db, sub, supply_address={"text": "Via Example 3"})
        with pytest.raises(BoundaryUnavailableError):
            await _save(db, sub, status=SubmissionStatus.SUBMITTED)

        assert "Via Example 3" not in caplog.text

    def test_the_migration_adds_and_drops_the_column(self, monkeypatch):
        """
        @verifies REQ-0018
        """
        from test_sharing_intent import _offline_sql

        up = _offline_sql(monkeypatch, "0015:0016")
        assert "ALTER TABLE submissions ADD COLUMN supply_address TEXT" in up
        down = _offline_sql(monkeypatch, "0016:0015", downgrade=True)
        assert "DROP COLUMN supply_address" in down


class TestScanningOffEndToEnd:
    """A boundary community with document scanning off: the wizard saves the
    address it checked, submits through the real route, and the submission is
    approved into the boundary's area. Before D48 it could not be submitted."""

    @pytest.fixture()
    def wizard(self, rec, db, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from celine.onboarding.api import submissions as api
        from celine.onboarding.config.settings import settings
        from celine.onboarding.models.database import get_db

        monkeypatch.setattr(settings, "extraction_enabled", False)
        assert not settings.document_processing_enabled

        sub = _submission(address=None)
        db.current.current = sub

        async def _live(submission_id, request, *, rec_slug=None, db=None):
            return sub

        async def _db():
            yield db

        monkeypatch.setattr(api, "_get_live_submission", _live)
        app = FastAPI()
        app.include_router(api.router, prefix="/api/{rec_slug}")
        app.dependency_overrides[get_db] = _db
        return TestClient(app), sub

    async def test_it_submits_and_is_approved(
        self, wizard, registry, fake_dt, geocoder, monkeypatch
    ):
        """
        @verifies REQ-0018
        @verifies REQ-0007
        @verifies REQ-0008
        """
        from test_enablement import FakeDb as EnablementDb

        from celine.onboarding.models.enablement import EnablementStatus, EnablementStep
        from celine.onboarding.services import audit_service, document_service, enablement, review
        from celine.onboarding.services.audit_service import Actor

        client, sub = wizard
        url = f"/api/rec-b/submissions/{uuid.uuid4()}"

        # The eligibility step's save, then the submit.
        assert (
            client.patch(url, json={"supply_address": {"text": "Via Example 3"}}).status_code == 200
        )
        response = client.patch(url, json={"status": "submitted"})
        assert response.status_code == 200, response.text
        assert sub.status == SubmissionStatus.SUBMITTED
        assert sub.extracted_data is None
        assert "AC000E0000" not in response.text

        # Approval, with only the registry step of the pipeline (the others need
        # services this test does not stand up).
        monkeypatch.setattr(
            enablement, "PIPELINE", (enablement.SPECS[EnablementStep.REC_REGISTRY_MEMBER],)
        )
        monkeypatch.setattr(audit_service, "record", lambda db, **kw: None)

        # The enablement fake answers every query with step rows; with scanning
        # off there is nothing uploaded to discard.
        async def _no_documents(db, submission):
            return 0

        monkeypatch.setattr(document_service, "discard_documents", _no_documents)
        from celine.onboarding.models.verification import (
            SubmissionVerification,
            VerificationMethod,
        )

        sub.verifications.append(
            SubmissionVerification(
                submission_id=sub.id,
                method=VerificationMethod.OFFLINE,
                actor_type="user",
                created_at=datetime.now(UTC),
            )
        )
        edb = EnablementDb()
        operator = Actor.system("test")
        await review.transition(edb, sub, SubmissionStatus.UNDER_REVIEW, actor=operator)
        await review.transition(edb, sub, SubmissionStatus.APPROVED, actor=operator)

        assert sub.status == SubmissionStatus.APPROVED
        [(community, body)] = registry
        assert (community, body["area"]) == ("example-rec", "south")
        assert sub.supply_boundary_id == "AC000E00003"
        rows = await enablement.load_steps(edb, sub.id)
        assert rows[EnablementStep.REC_REGISTRY_MEMBER].status == EnablementStatus.SUCCEEDED
