from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import celine.onboarding.services.otp as otp_service
from celine.onboarding.services import sms
from celine.onboarding.validators.phone import InvalidPhoneNumber, normalize_mobile, normalize_phone

# ── phone normalization (2.11) ───────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    ["+39 333 1234567", "3331234567", "333 123 4567", "+393331234567", " 333-1234567 "],
)
def test_italian_mobile_variants_normalize_to_one_value(raw):
    assert normalize_mobile(raw) == "+393331234567"


def test_foreign_number_keeps_its_prefix():
    assert normalize_phone("+33 6 12 34 56 78") == "+33612345678"


def test_landline_rejected_as_non_mobile():
    with pytest.raises(InvalidPhoneNumber, match="mobile"):
        normalize_mobile("+390212345678")


@pytest.mark.parametrize("raw", ["", "   ", "abc", "12"])
def test_invalid_numbers_rejected(raw):
    with pytest.raises(InvalidPhoneNumber):
        normalize_phone(raw)


# ── code generation and hashing ──────────────────────────────────


def test_generated_code_has_configured_length():
    for _ in range(50):
        code = otp_service.generate_code()
        assert len(code) == otp_service.settings.otp_code_length
        assert code.isdigit()


def test_code_hash_is_bound_to_phone():
    """The same code for a different number must not produce the same hash."""
    assert otp_service.hash_code("+393331234567", "123456") != otp_service.hash_code(
        "+393339999999", "123456"
    )


def test_phone_hash_is_deterministic():
    assert otp_service.hash_phone("+393331234567") == otp_service.hash_phone("+393331234567")


# ── OTP lifecycle against a fake session ─────────────────────────


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value


class FakeSession:
    """Minimal AsyncSession stand-in.

    `rows` is the OTP table; queries are dispatched on whether the statement is a
    count (send quota) or a select of the newest row.
    """

    def __init__(self, rows=None):
        self.rows = rows or []
        self.commits = 0

    async def execute(self, stmt):
        text = str(stmt)
        if "count" in text.lower():
            return FakeResult(len(self._recent()))
        return FakeResult(self._latest())

    def _recent(self):
        window = datetime.now(timezone.utc) - timedelta(hours=1)
        return [r for r in self.rows if r.created_at >= window]

    def _latest(self):
        return max(self.rows, key=lambda r: r.created_at, default=None)

    def add(self, row):
        self.rows.append(row)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _row):
        return None


class CapturingSms:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_otp(self, phone, code):
        if self.fail:
            raise sms.SmsDeliveryError("provider down")
        self.sent.append((phone, code))
        return True


@pytest.fixture()
def provider(monkeypatch):
    p = CapturingSms()
    monkeypatch.setattr(sms, "get_provider", lambda: p)
    monkeypatch.setattr(otp_service.sms, "get_provider", lambda: p)
    return p


PHONE = "+393331234567"


def _make_row(**kw):
    now = datetime.now(timezone.utc)
    row = otp_service.PhoneOtp(
        submission_id=kw.get("submission_id", uuid.uuid4()),
        phone=PHONE,
        phone_hash=otp_service.hash_phone(PHONE),
        code_hash=kw.get("code_hash", otp_service.hash_code(PHONE, "111111")),
        expires_at=kw.get("expires_at", now + timedelta(seconds=600)),
    )
    row.attempts = kw.get("attempts", 0)
    row.created_at = kw.get("created_at", now)
    row.verified_at = kw.get("verified_at")
    return row


async def test_send_otp_delivers_and_stores_hash_only(provider):
    db = FakeSession()
    sub_id = uuid.uuid4()

    otp = await otp_service.send_otp(db, sub_id, PHONE)

    assert len(provider.sent) == 1
    phone, code = provider.sent[0]
    assert phone == PHONE
    # the plaintext code must not be recoverable from the row
    assert code not in (otp.code_hash, otp.phone_hash)
    assert otp.code_hash == otp_service.hash_code(PHONE, code)


async def test_send_otp_not_persisted_when_delivery_fails(monkeypatch):
    """A failed send must not consume the user's hourly quota."""
    db = FakeSession()
    failing = CapturingSms(fail=True)
    monkeypatch.setattr(otp_service.sms, "get_provider", lambda: failing)

    with pytest.raises(sms.SmsDeliveryError):
        await otp_service.send_otp(db, uuid.uuid4(), PHONE)

    assert db.commits == 0


async def test_send_quota_enforced(provider, monkeypatch):
    monkeypatch.setattr(otp_service.settings, "otp_max_sends_per_hour", 3)
    db = FakeSession([_make_row(), _make_row(), _make_row()])

    with pytest.raises(otp_service.RateLimited):
        await otp_service.send_otp(db, uuid.uuid4(), PHONE)


async def test_old_sends_fall_out_of_the_window(provider, monkeypatch):
    monkeypatch.setattr(otp_service.settings, "otp_max_sends_per_hour", 3)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    db = FakeSession([_make_row(created_at=old, verified_at=old) for _ in range(3)])

    await otp_service.send_otp(db, uuid.uuid4(), PHONE)
    assert len(provider.sent) == 1


async def test_verify_success_marks_verified():
    db = FakeSession([_make_row(code_hash=otp_service.hash_code(PHONE, "111111"))])

    result = await otp_service.verify_otp(db, uuid.uuid4(), PHONE, "111111")

    assert result.verified_at is not None


async def test_verify_wrong_code_counts_attempt():
    row = _make_row()
    db = FakeSession([row])

    with pytest.raises(otp_service.InvalidCode, match="2 attempt"):
        await otp_service.verify_otp(db, uuid.uuid4(), PHONE, "999999")

    assert row.attempts == 1
    assert db.commits == 1  # the attempt is persisted even though verification failed


async def test_verify_locks_after_max_attempts(monkeypatch):
    monkeypatch.setattr(otp_service.settings, "otp_max_attempts", 3)
    row = _make_row(attempts=3)
    db = FakeSession([row])

    with pytest.raises(otp_service.Locked):
        await otp_service.verify_otp(db, uuid.uuid4(), PHONE, "111111")


async def test_correct_code_rejected_once_locked(monkeypatch):
    """Exhausting attempts must invalidate the code even if the guess is right."""
    monkeypatch.setattr(otp_service.settings, "otp_max_attempts", 3)
    row = _make_row(attempts=3, code_hash=otp_service.hash_code(PHONE, "111111"))
    db = FakeSession([row])

    with pytest.raises(otp_service.Locked):
        await otp_service.verify_otp(db, uuid.uuid4(), PHONE, "111111")


async def test_send_blocked_while_locked(provider, monkeypatch):
    """Lockout must not be escapable by requesting a fresh code."""
    monkeypatch.setattr(otp_service.settings, "otp_max_attempts", 3)
    monkeypatch.setattr(otp_service.settings, "otp_lockout_seconds", 3600)
    db = FakeSession([_make_row(attempts=3)])

    with pytest.raises(otp_service.Locked):
        await otp_service.send_otp(db, uuid.uuid4(), PHONE)

    assert provider.sent == []


async def test_send_allowed_after_lockout_expires(provider, monkeypatch):
    monkeypatch.setattr(otp_service.settings, "otp_max_attempts", 3)
    monkeypatch.setattr(otp_service.settings, "otp_lockout_seconds", 3600)
    monkeypatch.setattr(otp_service.settings, "otp_max_sends_per_hour", 3)
    long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    db = FakeSession([_make_row(attempts=3, created_at=long_ago)])

    await otp_service.send_otp(db, uuid.uuid4(), PHONE)
    assert len(provider.sent) == 1


async def test_verify_expired_code():
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    db = FakeSession([_make_row(expires_at=past)])

    with pytest.raises(otp_service.Expired):
        await otp_service.verify_otp(db, uuid.uuid4(), PHONE, "111111")


async def test_verify_without_any_code():
    db = FakeSession([])

    with pytest.raises(otp_service.InvalidCode, match="No code"):
        await otp_service.verify_otp(db, uuid.uuid4(), PHONE, "111111")


async def test_code_cannot_be_reused():
    row = _make_row(verified_at=datetime.now(timezone.utc))
    db = FakeSession([row])

    with pytest.raises(otp_service.InvalidCode, match="already been used"):
        await otp_service.verify_otp(db, uuid.uuid4(), PHONE, "111111")


# ── providers ────────────────────────────────────────────────────


async def test_log_provider_does_not_raise():
    assert await sms.LogSmsProvider().send_otp(PHONE, "123456") is True


async def test_brevo_requires_api_key():
    with pytest.raises(sms.SmsDeliveryError, match="BREVO_API_KEY"):
        await sms.BrevoSmsProvider(api_key="", sender="CELINE").send_otp(PHONE, "123456")


async def test_brevo_requires_sender():
    with pytest.raises(sms.SmsDeliveryError, match="BREVO_SMS_SENDER"):
        await sms.BrevoSmsProvider(api_key="k", sender="").send_otp(PHONE, "123456")


def test_get_provider_selects_by_setting(monkeypatch):
    monkeypatch.setattr(sms.settings, "sms_provider", "log")
    assert isinstance(sms.get_provider(), sms.LogSmsProvider)
    monkeypatch.setattr(sms.settings, "sms_provider", "brevo")
    assert isinstance(sms.get_provider(), sms.BrevoSmsProvider)
    monkeypatch.setattr(sms.settings, "sms_provider", "nope")
    with pytest.raises(sms.SmsDeliveryError, match="Unknown SMS_PROVIDER"):
        sms.get_provider()
