"""OTP generation, storage and verification.

Security model (Block 5.3):
- codes are never stored, only a keyed hash
- at most OTP_MAX_SENDS_PER_HOUR codes per phone per hour
- at most OTP_MAX_ATTEMPTS guesses per code, then the phone is locked for
  OTP_LOCKOUT_SECONDS
- codes expire after OTP_TTL_SECONDS
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.config.settings import settings
from celine.onboarding.models.phone_otp import PhoneOtp
from celine.onboarding.services import sms

logger = logging.getLogger(__name__)


class OtpError(RuntimeError):
    """Base for OTP failures that map to a 4xx."""


class RateLimitedError(OtpError):
    pass


class LockedError(OtpError):
    pass


class InvalidCodeError(OtpError):
    pass


class ExpiredError(OtpError):
    pass


def _hmac_key() -> bytes:
    """Key for the phone/code hashes.

    ENCRYPTION_KEY is already mandatory in production, so reuse it rather than
    introducing a second secret to manage. In dev without a key the hashes are
    unkeyed — acceptable because there is no real PII to protect there.
    """
    return (settings.encryption_key or "").encode("utf-8")


def hash_phone(e164: str) -> str:
    return hmac.new(_hmac_key(), e164.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_code(e164: str, code: str) -> str:
    """Bind the code hash to the phone so a hash cannot be replayed elsewhere."""
    msg = f"{e164}:{code}".encode()
    return hmac.new(_hmac_key(), msg, hashlib.sha256).hexdigest()


def generate_code() -> str:
    """A cryptographically random code, zero-padded to the configured length."""
    upper = 10**settings.otp_code_length
    return str(secrets.randbelow(upper)).zfill(settings.otp_code_length)


def _now() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    """Postgres returns tz-aware datetimes; be defensive for naive ones."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _latest_otp(db: AsyncSession, phone_hash: str) -> PhoneOtp | None:
    result = await db.execute(
        select(PhoneOtp)
        .where(PhoneOtp.phone_hash == phone_hash)
        .order_by(PhoneOtp.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _assert_not_locked(db: AsyncSession, phone_hash: str) -> None:
    latest = await _latest_otp(db, phone_hash)
    if latest is None or latest.verified_at is not None:
        return
    if latest.attempts < settings.otp_max_attempts:
        return

    unlock_at = _as_aware(latest.created_at) + timedelta(seconds=settings.otp_lockout_seconds)
    if _now() < unlock_at:
        raise LockedError("Too many incorrect codes. Try again later.")


async def _assert_send_quota(db: AsyncSession, phone_hash: str) -> None:
    window_start = _now() - timedelta(hours=1)
    result = await db.execute(
        select(func.count())
        .select_from(PhoneOtp)
        .where(PhoneOtp.phone_hash == phone_hash, PhoneOtp.created_at >= window_start)
    )
    if (result.scalar_one() or 0) >= settings.otp_max_sends_per_hour:
        raise RateLimitedError("Too many codes requested for this number. Try again later.")


async def send_otp(db: AsyncSession, submission_id, e164: str) -> PhoneOtp:
    """Issue and deliver a new OTP. Raises RateLimitedError / LockedError / SmsDeliveryError."""
    phone_hash = hash_phone(e164)

    await _assert_not_locked(db, phone_hash)
    await _assert_send_quota(db, phone_hash)

    code = generate_code()
    otp = PhoneOtp(
        submission_id=submission_id,
        phone=e164,
        phone_hash=phone_hash,
        code_hash=hash_code(e164, code),
        expires_at=_now() + timedelta(seconds=settings.otp_ttl_seconds),
    )
    db.add(otp)

    # Deliver before committing: if the provider rejects the message we must not
    # leave a row behind that counts against the user's hourly send quota.
    await sms.get_provider().send_otp(e164, code)

    await db.commit()
    await db.refresh(otp)
    return otp


async def verify_otp(db: AsyncSession, submission_id, e164: str, code: str) -> PhoneOtp:
    """Check `code` against the newest outstanding OTP for `e164`.

    Raises LockedError / ExpiredError / InvalidCodeError. Returns the verified row on success.
    """
    phone_hash = hash_phone(e164)
    otp = await _latest_otp(db, phone_hash)

    if otp is None:
        raise InvalidCodeError("No code has been requested for this number")
    if otp.verified_at is not None:
        raise InvalidCodeError("This code has already been used")

    if otp.attempts >= settings.otp_max_attempts:
        unlock_at = _as_aware(otp.created_at) + timedelta(seconds=settings.otp_lockout_seconds)
        if _now() < unlock_at:
            raise LockedError("Too many incorrect codes. Try again later.")
        raise InvalidCodeError("No code has been requested for this number")

    if _now() > _as_aware(otp.expires_at):
        raise ExpiredError("This code has expired. Request a new one.")

    # Count the attempt before comparing, and commit it even on failure, so a
    # crash or disconnect mid-verify cannot be used to retry without cost.
    otp.attempts += 1
    expected = otp.code_hash

    if not hmac.compare_digest(expected, hash_code(e164, code)):
        await db.commit()
        remaining = max(0, settings.otp_max_attempts - otp.attempts)
        logger.info(
            "OTP verification failed for submission %s (%d attempts remaining)",
            submission_id,
            remaining,
        )
        raise InvalidCodeError(f"Incorrect code. {remaining} attempt(s) remaining.")

    otp.verified_at = _now()
    await db.commit()
    await db.refresh(otp)
    return otp
