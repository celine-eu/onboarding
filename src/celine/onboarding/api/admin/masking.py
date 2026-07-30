"""Masking the identifiers an operator does not need in order to work a queue.

The repository encrypts `fiscal_code` and `pod_code` at rest with Fernet, then
until now handed both back in clear to anyone holding the admin token. Encryption
at rest that is undone at the last hop protects the backup tape and nothing else.

Masked by default, revealed on request by an operator holding
`submissions.reveal`, and every reveal is written to the audit trail. The point is
not that a `viewers` operator must never see a fiscal code — sometimes they must —
but that doing so is a deliberate act with their name on it.
"""

from __future__ import annotations

MASKED_FIELDS = ("fiscal_code", "pod_code")

# Enough to tell two records apart, not enough to be the identifier. A codice
# fiscale ends in a checksum character and a POD in the supply-point serial, so
# the tail is the discriminating part in both.
VISIBLE_TAIL = 4


def mask_value(value: str | None, *, tail: int = VISIBLE_TAIL) -> str | None:
    """`RSSMRA85T10A562S` → `••••••••••••562S`.

    Length is preserved: a truncated mask would make a malformed code look like a
    well-formed one, and "the POD is too short" is exactly the kind of thing an
    operator is looking for.
    """
    if not value:
        return value
    if len(value) <= tail:
        return "•" * len(value)
    return "•" * (len(value) - tail) + value[-tail:]


def mask_submission(payload: dict) -> dict:
    """Mask in place on a serialised submission."""
    for field in MASKED_FIELDS:
        if field in payload:
            payload[field] = mask_value(payload[field])
    return payload
