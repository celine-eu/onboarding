"""SMS delivery providers.

The OTP flow depends only on the `SmsProvider` protocol, so swapping Brevo for
another gateway is a settings change rather than a code change.
"""
from __future__ import annotations

import logging
from typing import Protocol

import httpx

from celine.onboarding.config.settings import settings

logger = logging.getLogger(__name__)

BREVO_SMS_URL = "https://api.brevo.com/v3/transactionalSMS/sms"


class SmsDeliveryError(RuntimeError):
    """Raised when the provider rejected or failed to accept the message."""


class SmsProvider(Protocol):
    async def send_otp(self, phone: str, code: str) -> bool:
        """Send `code` to `phone` (E.164). Return True when accepted for delivery."""
        ...


def _otp_message(code: str) -> str:
    return settings.sms_otp_template.format(code=code)


class BrevoSmsProvider:
    """Brevo (ex-Sendinblue) Transactional SMS."""

    def __init__(self, api_key: str = "", sender: str = "") -> None:
        self.api_key = api_key or settings.brevo_api_key
        self.sender = sender or settings.brevo_sms_sender

    async def send_otp(self, phone: str, code: str) -> bool:
        if not self.api_key:
            raise SmsDeliveryError("BREVO_API_KEY is not configured")
        if not self.sender:
            raise SmsDeliveryError("BREVO_SMS_SENDER is not configured")

        payload = {
            "sender": self.sender,
            "recipient": phone,
            "content": _otp_message(code),
            # Brevo classifies OTP traffic separately from marketing; transactional
            # messages bypass unsubscribe handling and are delivered out-of-hours.
            "type": "transactional",
        }
        headers = {"api-key": self.api_key, "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(BREVO_SMS_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise SmsDeliveryError(f"Brevo request failed: {exc}") from exc

        if resp.status_code >= 400:
            # Never log the response body: Brevo echoes the recipient number back.
            raise SmsDeliveryError(f"Brevo rejected the message ({resp.status_code})")

        return True


class LogSmsProvider:
    """Development provider — writes the OTP to the log instead of sending it."""

    async def send_otp(self, phone: str, code: str) -> bool:
        logger.warning("[LogSmsProvider] OTP for %s is %s (not actually sent)", phone, code)
        return True


def get_provider() -> SmsProvider:
    name = settings.sms_provider.strip().lower()
    if name == "brevo":
        return BrevoSmsProvider()
    if name in {"log", "console", "dev"}:
        return LogSmsProvider()
    raise SmsDeliveryError(f"Unknown SMS_PROVIDER: {settings.sms_provider!r}")
