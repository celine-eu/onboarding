import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.api.deps import limiter, valid_rec_slug
from celine.onboarding.models.database import get_db
from celine.onboarding.models.schemas import (
    PhoneConfirmRequest,
    PhoneVerifyRequest,
    PhoneVerifyStatus,
)
from celine.onboarding.services import otp as otp_service
from celine.onboarding.services.sms import SmsDeliveryError
from celine.onboarding.validators.phone import InvalidPhoneNumber, normalize_mobile

router = APIRouter(prefix="/submissions", tags=["phone-verification"])


def _resolve_phone(raw: str | None, submission) -> str:
    candidate = (raw or "").strip() or (submission.phone or "")
    try:
        return normalize_mobile(candidate)
    except InvalidPhoneNumber as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{submission_id}/verify-phone", response_model=PhoneVerifyStatus)
@limiter.limit("10/hour")
async def verify_phone(
    request: Request,
    submission_id: uuid.UUID,
    data: PhoneVerifyRequest,
    rec_slug: str = Depends(valid_rec_slug),
    db: AsyncSession = Depends(get_db),
):
    """Send an OTP to the submission's phone number."""
    from celine.onboarding.api.submissions import _get_live_submission

    submission = await _get_live_submission(
        submission_id, request, rec_slug=rec_slug, db=db
    )

    e164 = _resolve_phone(data.phone, submission)

    try:
        await otp_service.send_otp(db, submission.id, e164)
    except otp_service.Locked as exc:
        raise HTTPException(429, str(exc)) from exc
    except otp_service.RateLimited as exc:
        raise HTTPException(429, str(exc)) from exc
    except SmsDeliveryError as exc:
        # Do not leak provider internals to the client.
        raise HTTPException(502, "Could not send the verification code") from exc

    return PhoneVerifyStatus(phone_verified=False, sent=True)


@router.post("/{submission_id}/confirm-phone", response_model=PhoneVerifyStatus)
@limiter.limit("20/hour")
async def confirm_phone(
    request: Request,
    submission_id: uuid.UUID,
    data: PhoneConfirmRequest,
    rec_slug: str = Depends(valid_rec_slug),
    db: AsyncSession = Depends(get_db),
):
    """Confirm the OTP and mark the submission's phone as verified."""
    from celine.onboarding.api.submissions import _get_live_submission

    submission = await _get_live_submission(
        submission_id, request, rec_slug=rec_slug, db=db
    )

    e164 = _resolve_phone(data.phone, submission)

    try:
        verified = await otp_service.verify_otp(db, submission.id, e164, data.code)
    except otp_service.Locked as exc:
        raise HTTPException(429, str(exc)) from exc
    except otp_service.Expired as exc:
        raise HTTPException(410, str(exc)) from exc
    except otp_service.InvalidCode as exc:
        raise HTTPException(400, str(exc)) from exc

    # Persist the normalized number so the verified value is the stored value.
    submission.phone = e164
    submission.phone_verified = True
    submission.phone_verified_at = verified.verified_at
    await db.commit()

    return PhoneVerifyStatus(
        phone_verified=True, sent=False, phone_verified_at=submission.phone_verified_at
    )
