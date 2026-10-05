"""Encryption of personal data at rest.

**Keys.** ``ENCRYPTION_KEY`` holds one Fernet key, or several separated by commas.
The first encrypts; every key in the list decrypts (``MultiFernet``). Rotating is:
put a new key first, keep the old one after it, run
``onboarding-cli rotate-encryption-key`` to re-encrypt every column and stored
document with the new key, then drop the old key.

**Failures are loud.** A value that is a Fernet token and that no configured key
opens raises `DecryptionError` and logs an error. It is never handed back as if
it were the plaintext: doing so put ciphertext into exports, the PDF and the
registry payload with nothing saying anything was wrong.

**Plaintext rows.** A value that is not a Fernet token at all was written while
no key was configured (`REQUIRE_ENCRYPTION=false`, development only). It is read
as it is, with a warning, and ``rotate-encryption-key`` encrypts it.
"""

from __future__ import annotations

import base64
import binascii
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from celine.onboarding.config.settings import settings

logger = logging.getLogger(__name__)

# version byte + timestamp (8) + IV (16) + HMAC (32), then whole AES blocks.
_FERNET_VERSION = 0x80
_FERNET_OVERHEAD = 1 + 8 + 16 + 32
_AES_BLOCK = 16


class DecryptionError(RuntimeError):
    """A Fernet token that none of the configured keys opens."""


_fernet: MultiFernet | None = None
_checked = False


def parse_keys(raw: str) -> list[Fernet]:
    """The keys in ``raw``, in order. Raises `ValueError` on a malformed one."""
    keys = []
    for position, part in enumerate(p.strip() for p in (raw or "").split(",")):
        if not part:
            continue
        try:
            keys.append(Fernet(part.encode()))
        except (ValueError, binascii.Error) as exc:
            # The position, never the value: this message reaches the logs.
            raise ValueError(f"ENCRYPTION_KEY entry #{position + 1} is not a Fernet key") from exc
    return keys


def _get_fernet() -> MultiFernet | None:
    global _fernet, _checked
    if _checked:
        return _fernet
    keys = parse_keys(settings.encryption_key)
    _checked = True
    if not keys:
        logger.warning("ENCRYPTION_KEY not set — PII stored unencrypted")
        _fernet = None
        return None
    _fernet = MultiFernet(keys)
    return _fernet


def reset() -> None:
    """Forget the cached keys, so the next call reads ``settings`` again."""
    global _fernet, _checked
    _fernet = None
    _checked = False


def looks_like_token(data: bytes) -> bool:
    """Whether ``data`` has the shape of a Fernet token.

    A plaintext value (a name, a JSON document, a PDF or a JPEG) does not: a
    Fernet token is URL-safe base64 of a 0x80 version byte, a fixed 57-byte
    overhead and whole AES blocks.
    """
    try:
        raw = base64.urlsafe_b64decode(data)
    except (binascii.Error, ValueError):
        return False
    body = len(raw) - _FERNET_OVERHEAD
    return bool(raw) and raw[0] == _FERNET_VERSION and body > 0 and body % _AES_BLOCK == 0


def _decrypt_bytes(data: bytes, f: MultiFernet | None) -> bytes:
    if not looks_like_token(data):
        if f is not None:
            logger.warning(
                "Read a value stored unencrypted; run `onboarding-cli rotate-encryption-key` "
                "to encrypt it"
            )
        return data
    if f is None:
        logger.error("An encrypted value was read with no ENCRYPTION_KEY configured")
        raise DecryptionError("encrypted value, but no ENCRYPTION_KEY is configured")
    try:
        return f.decrypt(data)
    except InvalidToken:
        logger.error(
            "An encrypted value could not be decrypted with any configured key — "
            "was a key removed from ENCRYPTION_KEY before rotate-encryption-key ran?"
        )
        raise DecryptionError("no configured ENCRYPTION_KEY decrypts this value") from None


def encrypt(data: bytes) -> bytes:
    f = _get_fernet()
    if f is None:
        return data
    return f.encrypt(data)


def decrypt(data: bytes) -> bytes:
    return _decrypt_bytes(data, _get_fernet())


def encrypt_str(value: str) -> str:
    f = _get_fernet()
    if f is None:
        return value
    return f.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_str(value: str) -> str:
    return _decrypt_bytes(value.encode("utf-8"), _get_fernet()).decode("utf-8")


def rotate(data: bytes) -> bytes | None:
    """``data`` encrypted with the first key, or ``None`` when it already is.

    A plaintext value is encrypted. Raises `DecryptionError` as `decrypt` does,
    and `RuntimeError` without a key to rotate to.
    """
    f = _get_fernet()
    if f is None:
        raise RuntimeError("ENCRYPTION_KEY is not set: there is nothing to rotate to")
    if not looks_like_token(data):
        return f.encrypt(data)
    primary = MultiFernet([parse_keys(settings.encryption_key)[0]])
    try:
        primary.decrypt(data)
        return None
    except InvalidToken:
        pass
    try:
        return f.rotate(data)
    except InvalidToken:
        raise DecryptionError("no configured ENCRYPTION_KEY decrypts this value") from None
