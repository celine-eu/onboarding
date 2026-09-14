"""Phone verification is a feature switch, not a condition for starting.

A real SMS gateway receives participants' phone numbers, so it is used only with a
processing agreement (`DPA_SMS_SIGNED`). Without one the service still starts,
`/config` tells the wizard to leave the `phone_verify` step out, the phone routes
refuse, and approval does not wait for a verification this deployment cannot
perform — saying so on the submission and in the audit trail.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import celine.onboarding.main as app_main
from celine.onboarding.api import deps
from celine.onboarding.config.settings import settings
from celine.onboarding.models.database import get_db
from celine.onboarding.models.submission import SubmissionStatus
from celine.onboarding.models.verification import VerificationMethod
from celine.onboarding.services import review
from celine.onboarding.services.audit_service import Actor


@pytest.fixture()
def sms(monkeypatch):
    def _set(provider: str, *, signed: bool = False):
        monkeypatch.setattr(settings, "sms_provider", provider)
        monkeypatch.setattr(settings, "dpa_sms_signed", signed)

    return _set


# ── the switch ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("provider", "signed", "enabled"),
    [
        ("log", False, True),
        ("dev", False, True),
        ("brevo", True, True),
        ("brevo", False, False),
        ("Brevo ", False, False),
        ("carrier-pigeon", True, False),
    ],
)
def test_a_real_gateway_needs_the_agreement(sms, provider, signed, enabled):
    sms(provider, signed=signed)
    assert settings.phone_verification_enabled is enabled


# ── startup ───────────────────────────────────────────────────────────────────


async def test_the_service_starts_with_a_real_gateway_and_no_agreement(
    sms, bind_rec, monkeypatch, tmp_path, caplog
):
    """This combination used to refuse to boot."""
    from celine.onboarding.services import template_service

    sms("brevo", signed=False)
    bind_rec("rec-a")["steps"] = ["consents", "personal", "phone_verify", "review"]

    async def _nothing():
        return None

    monkeypatch.setattr(template_service, "load_recs_from_db", _nothing)
    monkeypatch.setattr(app_main, "_validate_dataspace_config", _nothing)
    monkeypatch.setattr(app_main, "_validate_admin_config", lambda: None)
    monkeypatch.setattr(app_main, "_validate_provisioning_config", lambda: None)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "require_encryption", False)

    with caplog.at_level(logging.WARNING, logger=app_main.logger.name):
        async with app_main.lifespan(FastAPI()):
            pass

    assert "Phone verification is disabled" in caplog.text
    assert "DPA_SMS_SIGNED is not set" in caplog.text


def test_an_unknown_provider_is_named(sms, caplog):
    sms("carrier-pigeon")
    with caplog.at_level(logging.WARNING, logger=app_main.logger.name):
        app_main._warn_phone_verification()

    assert "'carrier-pigeon' is not a known provider" in caplog.text


def test_no_warning_when_it_is_on(sms, caplog):
    sms("brevo", signed=True)
    with caplog.at_level(logging.WARNING, logger=app_main.logger.name):
        app_main._warn_phone_verification()

    assert caplog.text == ""


# ── what the wizard is told, and what the API refuses ─────────────────────────


@pytest.fixture()
def client(bind_rec):
    from celine.onboarding.api.config import router as config_router
    from celine.onboarding.api.phone_verify import router as phone_router

    bind_rec("rec-a")
    app = FastAPI()
    app.state.limiter = deps.limiter
    for router in (config_router, phone_router):
        app.include_router(router, prefix="/api/{rec_slug}")

    async def _no_db():
        yield None

    app.dependency_overrides[get_db] = _no_db
    return TestClient(app)


PHONE_ROUTES = [
    (f"/api/rec-a/submissions/{uuid.uuid4()}/verify-phone", {"phone": None}),
    (f"/api/rec-a/submissions/{uuid.uuid4()}/confirm-phone", {"code": "123456"}),
]


@pytest.mark.parametrize(
    ("provider", "signed", "enabled"), [("brevo", True, True), ("brevo", False, False)]
)
def test_config_reports_the_switch(sms, client, provider, signed, enabled):
    sms(provider, signed=signed)
    assert client.get("/api/rec-a/config").json()["features"]["phone_verification"] is enabled


@pytest.mark.parametrize(("url", "body"), PHONE_ROUTES)
def test_off_the_phone_routes_answer_403(sms, client, url, body):
    sms("brevo", signed=False)
    res = client.post(url, json=body)

    assert res.status_code == 403
    assert res.json()["detail"]["code"] == deps.PHONE_VERIFICATION_DISABLED


@pytest.mark.parametrize(("url", "body"), PHONE_ROUTES)
def test_on_the_phone_routes_get_past_the_switch(sms, client, monkeypatch, url, body):
    """Past the gate, each looks the submission up and finds nothing: 404, not 403."""
    from celine.onboarding.services import submission_service

    async def _none(*_a, **_kw):
        return None

    monkeypatch.setattr(submission_service, "get_submission", _none)
    sms("brevo", signed=True)

    assert client.post(url, json=body).status_code == 404


# ── the approval gate ─────────────────────────────────────────────────────────


def _submission(*, phone_verified: bool):
    return SimpleNamespace(
        id=uuid.uuid4(),
        rec_slug="rec-a",
        status=SubmissionStatus.UNDER_REVIEW,
        phone_verified=phone_verified,
        verification=SimpleNamespace(
            method="offline", verification_method=VerificationMethod.OFFLINE
        ),
    )


@pytest.fixture()
def phone_rec(bind_rec):
    bind_rec("rec-a")["steps"] = ["consents", "personal", "phone_verify", "review"]


def test_on_an_unverified_phone_still_blocks_approval(sms, phone_rec):
    sms("brevo", signed=True)
    submission = _submission(phone_verified=False)

    assert review.phone_verification_waived(submission) is False
    with pytest.raises(ValueError, match="phone number is not verified"):
        review.check(submission, SubmissionStatus.APPROVED)


def test_off_an_unverified_phone_is_waived(sms, phone_rec):
    sms("brevo", signed=False)
    submission = _submission(phone_verified=False)

    assert review.phone_verification_waived(submission) is True
    review.check(submission, SubmissionStatus.APPROVED)


def test_off_nothing_is_waived_where_nothing_was_asked(sms, bind_rec):
    bind_rec("rec-a")["steps"] = ["consents", "personal", "review"]
    sms("brevo", signed=False)

    assert review.phone_verification_waived(_submission(phone_verified=False)) is False


def test_off_a_verified_phone_is_not_a_waiver(sms, phone_rec):
    sms("brevo", signed=False)
    assert review.phone_verification_waived(_submission(phone_verified=True)) is False


class _Db:
    async def commit(self):
        pass

    async def refresh(self, _row):
        pass


async def test_the_waiver_is_in_the_approval_audit_row(sms, phone_rec, monkeypatch):
    from celine.onboarding.services import enablement

    recorded: list[dict] = []

    async def _enable(db, submission):
        return None

    monkeypatch.setattr(enablement, "enable", _enable)
    monkeypatch.setattr(review.audit_service, "record", lambda db, **kw: recorded.append(kw))
    sms("brevo", signed=False)
    submission = _submission(phone_verified=False)

    await review.transition(
        _Db(), submission, SubmissionStatus.APPROVED, actor=Actor.system("test")
    )

    assert submission.status == SubmissionStatus.APPROVED
    assert recorded[0]["detail"] == (
        "under_review -> approved (phone verification waived: disabled on this deployment)"
    )


def test_the_console_is_told(sms, phone_rec):
    from datetime import datetime

    from celine.onboarding.api.admin.submissions import _read
    from celine.onboarding.models.schemas import SubmissionAdminRead

    sms("brevo", signed=False)
    row = {name: None for name in SubmissionAdminRead.model_fields}
    row.update(
        id=uuid.uuid4(),
        ref="R-1",
        rec_slug="rec-a",
        status=SubmissionStatus.UNDER_REVIEW,
        gdpr_consent=True,
        policy_consent=True,
        statute_consent=True,
        data_sharing_consent=False,
        share_provisioned=False,
        keep_me_updated=False,
        phone_verified=False,
        consent_ip="127.0.0.1",
        created_at=datetime(2026, 9, 14),
        updated_at=datetime(2026, 9, 14),
        data_sharing_issues=[],
        phone_verification_waived=False,
    )

    assert _read(SimpleNamespace(**row)).phone_verification_waived is True
