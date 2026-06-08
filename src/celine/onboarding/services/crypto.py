from __future__ import annotations

import logging
import uuid

from cryptography.fernet import Fernet, InvalidToken

from celine.onboarding.config.settings import settings

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None
_checked = False


def _get_fernet() -> Fernet | None:
    global _fernet, _checked
    if _checked:
        return _fernet
    _checked = True
    key = settings.encryption_key
    if not key:
        logger.warning("ENCRYPTION_KEY not set — PII stored unencrypted")
        return None
    _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(data: bytes) -> bytes:
    f = _get_fernet()
    if f is None:
        return data
    return f.encrypt(data)


def decrypt(data: bytes) -> bytes:
    f = _get_fernet()
    if f is None:
        return data
    try:
        return f.decrypt(data)
    except InvalidToken:
        return data


def encrypt_str(value: str) -> str:
    f = _get_fernet()
    if f is None:
        return value
    return f.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_str(value: str) -> str:
    f = _get_fernet()
    if f is None:
        return value
    try:
        return f.decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return value


def generate_download_token(submission_id: uuid.UUID) -> str | None:
    f = _get_fernet()
    if f is None:
        return None
    return f.encrypt(str(submission_id).encode()).decode()


def validate_download_token(token: str, ttl: int | None = None) -> uuid.UUID | None:
    f = _get_fernet()
    if f is None:
        return None
    if ttl is None:
        ttl = settings.download_token_ttl
    try:
        payload = f.decrypt(token.encode(), ttl=ttl)
        return uuid.UUID(payload.decode())
    except (InvalidToken, ValueError):
        return None
