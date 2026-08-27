"""Phone number normalization to E.164.

OTPs are keyed by phone number, so the same number written two different ways
("333 1234567", "+39 333 1234567") must resolve to one identity — otherwise the
per-phone send limit and attempt lockout are trivially bypassed.
"""

import phonenumbers

DEFAULT_REGION = "IT"


class InvalidPhoneNumberError(ValueError):
    pass


def normalize_phone(raw: str, region: str = DEFAULT_REGION) -> str:
    """Return the number in E.164 form, or raise InvalidPhoneNumberError.

    Numbers without a country prefix are interpreted as belonging to `region`.
    """
    if not raw or not raw.strip():
        raise InvalidPhoneNumberError("Phone number is empty")

    try:
        parsed = phonenumbers.parse(raw.strip(), region)
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhoneNumberError(f"Cannot parse phone number: {exc}") from exc

    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneNumberError("Not a valid phone number")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def is_mobile(e164: str) -> bool:
    """True if the number is a mobile line.

    SMS to a landline silently disappears, so the API rejects non-mobile numbers
    up front rather than leaving the user waiting for a code that cannot arrive.
    """
    try:
        parsed = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return False

    return phonenumbers.number_type(parsed) in (
        phonenumbers.PhoneNumberType.MOBILE,
        phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE,
    )


def normalize_mobile(raw: str, region: str = DEFAULT_REGION) -> str:
    """Normalize and require a mobile line."""
    e164 = normalize_phone(raw, region)
    if not is_mobile(e164):
        raise InvalidPhoneNumberError("Not a mobile number — SMS cannot be delivered")
    return e164
