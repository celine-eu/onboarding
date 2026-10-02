"""An applicant who declares they are already a member of the community.

The template offers the declaration; a declared applicant skips the steps it names
and may submit without a POD or a supply address in the community's area. The
operator completes both from the community's register, by revision, and approval
waits for that. The declaration grants nothing else.
"""

# The fixtures imported from the revision and boundary suites are used by name,
# which ruff reads as redefinitions.
# ruff: noqa: F811

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_revision import (
    CORRECT_POD,
    ORG,
    SUBMISSION_ID,
    FakeDb,
    api,  # noqa: F401 — fixture
    auth,
    make_submission,
    revise,
    steps,  # noqa: F401 — fixture
    trail,  # noqa: F401 — fixture
)
from test_supply_boundary import (
    _save,
    _submission,
    db,  # noqa: F401 — fixture
    geocoder,  # noqa: F401 — fixture
)
from test_supply_boundary import (
    rec as boundary_rec,  # noqa: F401 — fixture
)

from celine.onboarding.models.enablement import EnablementStatus
from celine.onboarding.models.schemas import ConsentCreate, SubmissionAdminRead
from celine.onboarding.models.submission import SubmissionStatus
from celine.onboarding.models.verification import SubmissionVerification
from celine.onboarding.services import existing_member, review, submission_service
from celine.onboarding.services import template_service as ts
from celine.onboarding.services.revision import (
    RevisableField,
    RevisionError,
    RevisionStatusError,
)
from celine.onboarding.workflows.engine import can_submit

OFFERED = {"enabled": True, "skip_steps": ["energy"]}
#: The coverage step skipped too: the supply address is the operator's.
NO_COVERAGE = {"enabled": True, "skip_steps": ["energy", "eligibility"]}

ENERGY_FIELD = {"key": "has_pv", "label": "PV", "type": "boolean", "step": "energy"}


def _declared(submission):
    submission.declared_existing_member = True
    return submission


def _offer(seeded, block):
    """Give the boundary community (`rec`, already seeded) an `existing_members` block."""
    ts._cache["rec-b"]["existing_members"] = block


def _verified(submission):
    submission.verifications.append(
        SubmissionVerification(
            id=uuid.uuid4(),
            submission_id=submission.id,
            method="offline",
            actor_type="operator",
            created_at=datetime.now(UTC),
        )
    )
    return submission


# ── the template's offer ─────────────────────────────────────────────────────


class TestTheTemplateBlock:
    @pytest.mark.parametrize(
        "block",
        [
            None,
            {"enabled": False},
            OFFERED,
            {"enabled": True, "skip_steps": []},
            {"enabled": True, "skip_steps": ["phone_verify", "energy", "eligibility"]},
        ],
        ids=["absent", "off", "offered", "no-skip", "no-coverage"],
    )
    def test_a_valid_block_is_accepted(self, block):
        """
        @verifies REQ-0024
        """
        ts.validate_existing_members(block, where="t")

    @pytest.mark.parametrize(
        ("block", "match"),
        [
            ("yes", "must be a mapping"),
            ({"enabled": "yes"}, "true or false"),
            ({"enabled": True, "defer": ["pod_code"]}, "unknown keys"),
            ({"enabled": True, "skip_steps": "energy"}, "list of step names"),
            ({"enabled": True, "skip_steps": ["statute"]}, "may name only"),
            ({"enabled": True, "skip_steps": ["review"]}, "may name only"),
        ],
        ids=[
            "not-a-mapping",
            "enabled-not-bool",
            "unknown-key",
            "skip-not-list",
            "statute",
            "review",
        ],
    )
    def test_anything_else_is_refused(self, block, match):
        """
        @verifies REQ-0024
        """
        with pytest.raises(ValueError, match=match):
            ts.validate_existing_members(block, where="t")

    def test_the_config_says_whether_it_is_offered(self, seed_rec):
        """
        @verifies REQ-0024
        """
        seed_rec("rec-a", existing_members=OFFERED)
        seed_rec("rec-off")
        assert ts.get_config("rec-a")["existing_members"] == OFFERED
        assert ts.get_config("rec-off")["existing_members"] == {
            "enabled": False,
            "skip_steps": [],
        }


# ── the declaration ──────────────────────────────────────────────────────────


def _consent(**kw) -> ConsentCreate:
    return ConsentCreate(
        gdpr_consent=True,
        gdpr_consent_version="1",
        policy_consent=True,
        policy_consent_version="1",
        statute_consent=True,
        statute_consent_version="1",
        **kw,
    )


class _CreateDb(FakeDb):
    def add(self, obj):
        self.added = obj


class TestTheDeclaration:
    async def test_it_is_recorded_where_offered(self, seed_rec):
        """
        @verifies REQ-0024
        """
        seed_rec("rec-a", existing_members=OFFERED)
        db = _CreateDb()
        sub = await submission_service.create_from_consent(
            db, _consent(declared_existing_member=True), "198.51.100.7", "rec-a"
        )
        assert sub.declared_existing_member is True

    async def test_it_is_refused_where_not_offered(self, seed_rec):
        """
        @verifies REQ-0024
        """
        seed_rec("rec-a")
        with pytest.raises(existing_member.DeclarationNotOfferedError):
            await submission_service.create_from_consent(
                _CreateDb(), _consent(declared_existing_member=True), "198.51.100.7", "rec-a"
            )

    def test_the_wizard_route_answers_422(self, seed_rec, monkeypatch):
        """
        @verifies REQ-0024
        """
        from celine.onboarding.api import submissions as api
        from celine.onboarding.api.deps import valid_rec_slug
        from celine.onboarding.models.database import get_db

        seed_rec("rec-a")
        app = FastAPI()
        app.state.limiter = api.limiter
        app.include_router(api.router, prefix="/api/{rec_slug}")

        async def _db():
            yield _CreateDb()

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[valid_rec_slug] = lambda: "rec-a"
        res = TestClient(app).post(
            "/api/rec-a/submissions",
            json=_consent(declared_existing_member=True).model_dump(),
        )
        assert res.status_code == 422, res.text

    async def test_a_patch_cannot_declare_where_not_offered(self, seed_rec, db):
        """
        @verifies REQ-0024
        """
        seed_rec("rec-b")
        sub = _submission()
        with pytest.raises(existing_member.DeclarationNotOfferedError):
            await _save(db, sub, declared_existing_member=True)
        assert sub.declared_existing_member is not True


# ── at submit ────────────────────────────────────────────────────────────────


class TestAtSubmit:
    def test_a_declared_applicant_needs_no_pod(self, seed_rec):
        """
        @verifies REQ-0025
        """
        seed_rec("rec-b", existing_members=OFFERED)
        sub = _declared(_submission())
        sub.pod_code = None
        assert can_submit(sub) == []

    def test_an_undeclared_applicant_still_does(self, seed_rec):
        """
        @verifies REQ-0025
        """
        seed_rec("rec-b", existing_members=OFFERED)
        sub = _submission()
        sub.pod_code = None
        assert "pod_code is required" in can_submit(sub)

    def test_a_required_field_of_a_skipped_step_is_not_required(self, seed_rec):
        """
        @verifies REQ-0024
        """
        seed_rec(
            "rec-b",
            existing_members=OFFERED,
            fields={"extra": [{**ENERGY_FIELD, "required": True}]},
        )
        assert can_submit(_declared(_submission())) == []
        assert "has_pv is required" in can_submit(_submission())

    @pytest.mark.parametrize(
        "address",
        [None, "Via Nowhere 9", "Via Example 1", "Somewhere unknown"],
        ids=["no-address", "no-boundary", "undeclared-boundary", "not-found"],
    )
    async def test_without_the_coverage_step_the_address_refuses_nothing(
        self, boundary_rec, db, fake_dt, geocoder, address
    ):
        """
        @verifies REQ-0025
        """
        _offer(boundary_rec, NO_COVERAGE)
        sub = _declared(_submission(address))
        await _save(db, sub, status=SubmissionStatus.SUBMITTED)
        assert sub.status == SubmissionStatus.SUBMITTED

    async def test_without_the_coverage_step_an_unanswered_check_refuses_nothing(
        self, boundary_rec, db, fake_dt, geocoder
    ):
        """
        @verifies REQ-0025
        """
        _offer(boundary_rec, NO_COVERAGE)
        fake_dt.failure = httpx.ReadTimeout("slow")
        sub = _declared(_submission("Via Example 2"))
        await _save(db, sub, status=SubmissionStatus.SUBMITTED)
        assert sub.status == SubmissionStatus.SUBMITTED
        assert sub.supply_boundary_id is None

    async def test_an_address_it_holds_anyway_is_recorded_as_a_hint(
        self, boundary_rec, db, fake_dt, geocoder
    ):
        """
        @verifies REQ-0025
        """
        _offer(boundary_rec, NO_COVERAGE)
        sub = _declared(_submission("Via Example 2"))
        await _save(db, sub, status=SubmissionStatus.SUBMITTED)
        assert sub.supply_boundary_id == "AC000E00002"

    async def test_with_the_coverage_step_the_check_is_everyones(
        self, boundary_rec, db, fake_dt, geocoder
    ):
        """
        @verifies REQ-0025
        """
        _offer(boundary_rec, OFFERED)
        sub = _declared(_submission("Via Nowhere 9"))
        with pytest.raises(ValueError, match="not in the community's area"):
            await _save(db, sub, status=SubmissionStatus.SUBMITTED)
        assert sub.status == SubmissionStatus.DRAFT


# ── at approval ──────────────────────────────────────────────────────────────


def _under_review(**kw):
    sub = _verified(make_submission(SubmissionStatus.UNDER_REVIEW))
    sub.supply_address = {"text": "Via Example 2, Example Town"}
    for key, value in kw.items():
        setattr(sub, key, value)
    return sub


class TestAtApproval:
    def test_without_the_coverage_step_it_waits_for_the_pod_and_the_address(self, seed_rec):
        """
        @verifies REQ-0025
        """
        seed_rec("rec-a", existing_members=NO_COVERAGE)
        sub = _declared(_under_review(pod_code=None))
        assert existing_member.pending(sub) == ["pod_code", "supply_address"]
        with pytest.raises(ValueError, match="pod_code, supply_address"):
            review.check(sub, SubmissionStatus.APPROVED)

    async def test_an_address_the_operator_did_not_enter_is_not_enough(self, seed_rec, trail):
        """
        @verifies REQ-0025
        """
        seed_rec("rec-a", existing_members=NO_COVERAGE)
        sub = _declared(_under_review())
        assert existing_member.pending(sub) == ["supply_address"]
        with pytest.raises(ValueError, match="supply_address"):
            review.check(sub, SubmissionStatus.APPROVED)

    def test_with_the_coverage_step_only_the_pod_is_left_to_the_operator(self, seed_rec):
        """
        @verifies REQ-0025
        """
        seed_rec("rec-a", existing_members=OFFERED)
        sub = _declared(_under_review(pod_code=None))
        assert existing_member.pending(sub) == ["pod_code"]
        assert existing_member.pending(_declared(_under_review())) == []

    async def test_once_the_operator_completed_both_it_may_be_approved(self, seed_rec, trail):
        """
        @verifies REQ-0025
        """
        seed_rec("rec-a", existing_members=NO_COVERAGE)
        sub = _declared(_under_review(pod_code=None))
        await revise(sub, RevisableField.POD_CODE, CORRECT_POD)
        await revise(sub, RevisableField.SUPPLY_ADDRESS, "Via Example 3, Example Town")

        assert existing_member.pending(sub) == []
        review.check(sub, SubmissionStatus.APPROVED)

    def test_an_undeclared_applicant_is_unaffected(self, seed_rec):
        """
        @verifies REQ-0025
        """
        seed_rec("rec-a", existing_members=NO_COVERAGE)
        sub = _under_review()
        assert existing_member.pending(sub) == []
        review.check(sub, SubmissionStatus.APPROVED)

    @pytest.mark.parametrize("sms", [True, False], ids=["sms-on", "sms-off"])
    async def test_a_skipped_phone_step_is_not_required(self, seed_rec, trail, monkeypatch, sms):
        """
        @verifies REQ-0024
        """
        from celine.onboarding.config.settings import settings

        monkeypatch.setattr(
            type(settings), "phone_verification_enabled", property(lambda self: sms)
        )
        seed_rec(
            "rec-a",
            steps=["consents", "personal", "phone_verify", "review"],
            existing_members={"enabled": True, "skip_steps": ["phone_verify"]},
        )
        sub = _declared(_under_review())

        assert review.phone_verification_waived(sub) is False
        review.check(sub, SubmissionStatus.APPROVED)

        undeclared = _under_review()
        if sms:
            with pytest.raises(ValueError, match="phone number is not verified"):
                review.check(undeclared, SubmissionStatus.APPROVED)
        else:
            assert review.phone_verification_waived(undeclared) is True

    def test_the_admin_read_names_what_is_pending(self, seed_rec):
        """
        @verifies REQ-0025
        """
        from celine.onboarding.api.admin.submissions import _read

        seed_rec("rec-a", existing_members=NO_COVERAGE)
        read = _read(_declared(_under_review(pod_code=None)))
        assert isinstance(read, SubmissionAdminRead)
        assert read.declared_existing_member is True
        assert read.existing_member_pending == ["pod_code", "supply_address"]

    def test_the_queue_filters_on_the_declaration(self):
        """
        @verifies REQ-0025
        """
        from sqlalchemy import select

        from celine.onboarding.models.submission import Submission

        query = submission_service._queue_filters(
            select(Submission), rec_slug="rec-a", declared_existing_member=True
        )
        assert "submissions.declared_existing_member" in str(query)
        plain = submission_service._queue_filters(select(Submission), rec_slug="rec-a")
        assert "declared_existing_member =" not in str(plain.whereclause)


# ── revisions of the fiscal code and the supply address ─────────────────────


def _approved_and_enabled(steps_rows):
    steps_rows["keycloak_user"] = SimpleNamespace(
        status=EnablementStatus.SUCCEEDED, completed_at=datetime.now(UTC)
    )
    return make_submission(SubmissionStatus.APPROVED)


class TestRevisions:
    async def test_the_fiscal_code_is_revised_and_upper_cased(self, trail, steps):
        """
        @verifies REQ-0026
        """
        sub = make_submission()
        outcome = await revise(sub, RevisableField.FISCAL_CODE, "rssmra85t10a562s")
        assert sub.fiscal_code == "RSSMRA85T10A562S"
        assert outcome.previous_value is None

    async def test_a_bad_fiscal_code_is_refused(self, trail, steps):
        """
        @verifies REQ-0026
        """
        with pytest.raises(RevisionError, match="Invalid fiscal code"):
            await revise(make_submission(), RevisableField.FISCAL_CODE, "not-a-code")

    async def test_after_approval_the_fiscal_code_propagates_nowhere(self, trail, steps):
        """
        @verifies REQ-0026
        """
        sub = _approved_and_enabled(steps)
        outcome = await revise(sub, RevisableField.FISCAL_CODE, "RSSMRA85T10A562S")
        assert outcome.propagation == ()
        assert sub.fiscal_code == "RSSMRA85T10A562S"

    async def test_the_supply_address_is_stored_as_the_wizard_saves_it(
        self, seed_rec, trail, steps
    ):
        """
        @verifies REQ-0026
        """
        seed_rec("rec-a")
        sub = make_submission()
        sub.supply_address = {"text": "Example Town"}
        outcome = await revise(sub, RevisableField.SUPPLY_ADDRESS, "  Via Example 2  ")
        assert sub.supply_address == {"text": "Via Example 2"}
        assert (outcome.previous_value, outcome.new_value) == ("Example Town", "Via Example 2")

    async def test_the_supply_address_is_refused_after_approval(self, trail, steps):
        """
        @verifies REQ-0026
        """
        sub = _approved_and_enabled(steps)
        with pytest.raises(RevisionStatusError, match="only before approval"):
            await revise(sub, RevisableField.SUPPLY_ADDRESS, "Via Example 2")
        assert sub.revisions == []

    async def test_a_revised_address_resolves_its_boundary_again(
        self, boundary_rec, fake_dt, geocoder, trail, steps
    ):
        """
        @verifies REQ-0026
        """
        sub = _submission("Via Example 3")
        sub.status = SubmissionStatus.SUBMITTED
        sub.supply_address = {"text": "Via Example 3"}
        sub.supply_boundary_id = "AC000E00003"
        await revise(sub, RevisableField.SUPPLY_ADDRESS, "Via Example 2")
        assert sub.supply_boundary_id == "AC000E00002"

    def test_the_fiscal_code_is_masked_in_the_history(self, api, operator_token):
        """
        @verifies REQ-0026
        """
        client, _ = api
        url = f"/api/admin/rec-a/submissions/{SUBMISSION_ID}/revisions"
        body = {
            "field": "fiscal_code",
            "value": "RSSMRA85T10A562S",
            "method": "offline",
            "note": "from the register",
        }
        res = client.post(url, json=body, headers=auth(operator_token(ORG, "managers")))
        assert res.status_code == 201, res.text
        assert res.json()["new_value"] == "••••••••••••562S"

    @pytest.mark.parametrize("field", ["fiscal_code", "supply_address"])
    def test_the_admin_patch_refuses_them_once_submitted(
        self,
        api,
        operator_token,
        field,
    ):
        """
        @verifies REQ-0026
        """
        client, _ = api
        value = "RSSMRA85T10A562S" if field == "fiscal_code" else {"text": "Via Example 2"}
        res = client.patch(
            f"/api/admin/rec-a/submissions/{SUBMISSION_ID}",
            json={field: value},
            headers=auth(operator_token(ORG, "managers")),
        )
        assert res.status_code == 409, res.text


class TestTheMigration:
    def test_upgrade_adds_the_column_false_for_everyone(self, monkeypatch):
        """
        @verifies REQ-0024
        """
        from test_sharing_intent import _offline_sql

        sql = _offline_sql(monkeypatch, "0018:0019")
        assert "ALTER TABLE submissions ADD COLUMN declared_existing_member BOOLEAN" in sql
        assert "DEFAULT false NOT NULL" in sql

    def test_downgrade_drops_it(self, monkeypatch):
        from test_sharing_intent import _offline_sql

        sql = _offline_sql(monkeypatch, "0019:0018", downgrade=True)
        assert "DROP COLUMN declared_existing_member" in sql
