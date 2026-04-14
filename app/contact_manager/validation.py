"""Phone and email validation."""

from __future__ import annotations

import re
from typing import Optional, Tuple

import phonenumbers
from email_validator import EmailNotValidError, validate_email


def normalize_phone_ca_us(raw: str, default_region: str = "CA") -> Tuple[bool, Optional[str]]:
    """Return (ok, e164_or_none)."""
    s = raw.strip()
    if not s:
        return False, None
    try:
        num = phonenumbers.parse(s, default_region)
        if not phonenumbers.is_valid_number(num):
            return False, None
        return True, phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        return False, None


def validate_email_addr(raw: str) -> Tuple[bool, Optional[str]]:
    if not raw or not raw.strip():
        return False, None
    try:
        v = validate_email(raw.strip(), check_deliverability=False)
        return True, v.normalized
    except EmailNotValidError:
        return False, None
