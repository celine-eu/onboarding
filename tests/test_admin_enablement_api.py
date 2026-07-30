"""The enablement surface, through the API and its capability gates.

`tests/test_enablement.py` pins the pipeline's behaviour; this pins who may reach
it and what the console gets back.
"""

from __future__ import annotations

import uuid
from datetime import UTC

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from celine.onboarding.api.admin import create_admin_router
from celine.onboarding.models.enablement import EnablementStatus, EnablementStep
from celine.onboarding.security.middleware import AdminAuthMiddleware
from celine.onboarding.services import audit_service, enablement

ORG = "community-a"
SUBMISSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def stub_submission():
    """A real `Submission` instance, unpersisted.

    Built rather than faked because the endpoints serialise through
    `SubmissionAdminRead`, and a duck-typed stub would fail response validation
    for reasons that have nothing to do with what is being tested.
    """
    from datetime import datetime

    from celine.onboarding.models.submission import Submission, SubmissionStatus

    now = datetime.now(UTC)
    submission = Submission(
        id=SUBMISSION_ID,
        ref="20260730-aaa1",
        rec_slug="rec-a",
        status=SubmissionStatus.UNDER_REVIEW,
        consent_ip="10.0.0.1",
        session_token="stub",
    )
    for field, value in {
        "gdpr_consent": True,
        "policy_consent": True,
        "statute_consent": True,
        "keep_me_updated": False,
        "phone_verified": False,
        "phone_verified_at": None,
        "notes": None,
        "share_provisioned": False,
        "data_sharing_consent": False,
        "dataspace_subject_id": None,
        "dataspace_did": None,
        "dataspace_vc_id": None,
        "dataspace_vc_issued_at": None,
        "created_at": now,
        "updated_at": now,
        "last_active_at": now,
    }.items():
        setattr(submission, field, value)
    return submission


@pytest.fixture()
def stub_backend(monkeypatch, seed_rec):
    """Replace the data layer; these tests are about routing and authorization."""
    seed_rec("rec-a", name="REC A", organization=ORG)

    from celine.onboarding.api.admin import enablement as enablement_api
    from celine.onboarding.api.admin import submissions as submissions_api

    async def _owned(db, submission_id, rec_slug):
        from fastapi import HTTPException

        if submission_id != SUBMISSION_ID:
            raise HTTPException(404, "Submission not found")
        return stub_submission()

    monkeypatch.setattr(submissions_api, "_owned_submission", _owned)
    monkeypatch.setattr(enablement_api, "_owned_submission", _owned)

    async def _noop_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(audit_service, "record_and_commit", _noop_audit)

    calls: dict = {}

    def _rows(**overrides):
        from celine.onboarding.models.enablement import SubmissionEnablementStep

        return {
            spec.step: SubmissionEnablementStep(
                step=spec.step,
                status=overrides.get(spec.step, EnablementStatus.SUCCEEDED),
                attempts=1,
            )
            for spec in enablement.PIPELINE
        }

    async def _load(db, submission_id):
        return _rows(**{EnablementStep.DATASPACE_SHARE: EnablementStatus.FAILED})

    async def _retry(db, submission, *, step=None):
        if step is not None:
            enablement.spec_for(step)
        calls["retry"] = step
        return _rows()

    async def _revoke(db, submission):
        calls["revoke"] = True
        return _rows(
            **dict.fromkeys([s.step for s in enablement.PIPELINE], EnablementStatus.PENDING)
        )

    monkeypatch.setattr(enablement, "load_steps", _load)
    monkeypatch.setattr(enablement, "retry", _retry)
    monkeypatch.setattr(enablement, "revoke", _revoke)
    return calls


@pytest.fixture()
def client(stub_backend, issue_token) -> TestClient:
    app = FastAPI()
    app.add_middleware(AdminAuthMiddleware)
    app.include_router(create_admin_router())
    return TestClient(app)


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


BASE = f"/api/admin/rec-a/submissions/{SUBMISSION_ID}"


class TestRead:
    def test_returns_the_full_pipeline(self, client, operator_token):
        body = client.get(f"{BASE}/enablement", headers=auth(operator_token(ORG, "viewers"))).json()
        assert [s["step"] for s in body["steps"]] == [
            "keycloak_user",
            "rec_registry_member",
            "dataspace_identity",
            "dataspace_share",
        ]

    def test_reports_the_summary_state(self, client, operator_token):
        body = client.get(f"{BASE}/enablement", headers=auth(operator_token(ORG, "viewers"))).json()
        assert body["state"] == "failed"

    def test_says_which_steps_block_approval(self, client, operator_token):
        """The console needs to distinguish "blocked" from "worth retrying"."""
        body = client.get(f"{BASE}/enablement", headers=auth(operator_token(ORG, "viewers"))).json()
        closed = {s["step"]: s["fail_closed"] for s in body["steps"]}
        assert closed["rec_registry_member"] is True
        assert closed["dataspace_share"] is False

    def test_unknown_submission_is_404(self, client, operator_token):
        other = uuid.uuid4()
        response = client.get(
            f"/api/admin/rec-a/submissions/{other}/enablement",
            headers=auth(operator_token(ORG, "viewers")),
        )
        assert response.status_code == 404


class TestRetry:
    def test_viewer_cannot_retry(self, client, operator_token):
        response = client.post(
            f"{BASE}/enablement/retry", json={}, headers=auth(operator_token(ORG, "viewers"))
        )
        assert response.status_code == 403

    def test_editor_cannot_retry(self, client, operator_token):
        response = client.post(
            f"{BASE}/enablement/retry", json={}, headers=auth(operator_token(ORG, "editors"))
        )
        assert response.status_code == 403

    def test_manager_can_retry(self, client, operator_token, stub_backend):
        response = client.post(
            f"{BASE}/enablement/retry", json={}, headers=auth(operator_token(ORG, "managers"))
        )
        assert response.status_code == 200
        assert stub_backend["retry"] is None
        assert response.json()["state"] == "complete"

    def test_one_named_step(self, client, operator_token, stub_backend):
        response = client.post(
            f"{BASE}/enablement/retry",
            json={"step": "rec_registry_member"},
            headers=auth(operator_token(ORG, "managers")),
        )
        assert response.status_code == 200
        assert stub_backend["retry"] == "rec_registry_member"

    def test_unknown_step_is_422(self, client, operator_token):
        response = client.post(
            f"{BASE}/enablement/retry",
            json={"step": "teleport"},
            headers=auth(operator_token(ORG, "managers")),
        )
        assert response.status_code == 422

    def test_cross_community_retry_is_403(self, client, operator_token):
        response = client.post(
            f"{BASE}/enablement/retry",
            json={},
            headers=auth(operator_token("community-b", "admins")),
        )
        assert response.status_code == 403


class TestRevoke:
    def test_manager_cannot_revoke(self, client, operator_token):
        """Revocation deletes a credential and a registry member — admins only."""
        response = client.post(
            f"{BASE}/enablement/revoke", headers=auth(operator_token(ORG, "managers"))
        )
        assert response.status_code == 403

    def test_admin_can_revoke(self, client, operator_token, stub_backend):
        response = client.post(
            f"{BASE}/enablement/revoke", headers=auth(operator_token(ORG, "admins"))
        )
        assert response.status_code == 200
        assert stub_backend["revoke"] is True
        assert response.json()["state"] == "not_started"


class TestTransition:
    @pytest.fixture()
    def stub_review(self, monkeypatch):
        from celine.onboarding.api.admin import submissions as submissions_api

        recorded: dict = {}

        async def _transition(db, submission, target, **kwargs):
            recorded["target"] = target
            recorded.update(kwargs)
            submission.status = target
            return submission

        monkeypatch.setattr(submissions_api.review, "transition", _transition)
        return recorded

    def test_editor_cannot_transition(self, client, operator_token, stub_review):
        response = client.post(
            f"{BASE}/transition",
            json={"target": "approved"},
            headers=auth(operator_token(ORG, "editors")),
        )
        assert response.status_code == 403

    def test_rejection_requires_a_reason(self, client, operator_token, stub_review):
        """The participant is told, and whoever reopens the case months later needs it."""
        response = client.post(
            f"{BASE}/transition",
            json={"target": "rejected"},
            headers=auth(operator_token(ORG, "managers")),
        )
        assert response.status_code == 422
        assert "reason is required" in response.json()["detail"]

    def test_blank_reason_does_not_count(self, client, operator_token, stub_review):
        response = client.post(
            f"{BASE}/transition",
            json={"target": "rejected", "reason": "   "},
            headers=auth(operator_token(ORG, "managers")),
        )
        assert response.status_code == 422

    def test_reason_reaches_the_service(self, client, operator_token, stub_review):
        client.post(
            f"{BASE}/transition",
            json={"target": "rejected", "reason": "POD belongs to another supply"},
            headers=auth(operator_token(ORG, "managers")),
        )
        assert stub_review["reason"] == "POD belongs to another supply"

    def test_approval_needs_no_reason(self, client, operator_token, stub_review):
        response = client.post(
            f"{BASE}/transition",
            json={"target": "approved"},
            headers=auth(operator_token(ORG, "managers")),
        )
        assert response.status_code == 200
        assert stub_review["target"].value == "approved"

    def test_a_fail_closed_step_is_422(self, client, operator_token, monkeypatch):
        from celine.onboarding.api.admin import submissions as submissions_api
        from celine.onboarding.services.enablement import EnablementFailed

        async def _fails(db, submission, target, **kwargs):
            raise EnablementFailed(
                EnablementStep.REC_REGISTRY_MEMBER,
                "Community member could not be provisioned: registry said no",
            )

        monkeypatch.setattr(submissions_api.review, "transition", _fails)

        response = client.post(
            f"{BASE}/transition",
            json={"target": "approved"},
            headers=auth(operator_token(ORG, "managers")),
        )
        assert response.status_code == 422
        assert "registry said no" in response.json()["detail"]
