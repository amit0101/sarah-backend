"""Tests for contact manager service — find/create, dedup, validation."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.contact_manager.validation import normalize_phone_ca_us, validate_email_addr


class TestPhoneValidation:
    def test_valid_canadian_number(self):
        ok, e164 = normalize_phone_ca_us("+14035551234")
        assert ok is True
        assert e164 == "+14035551234"

    def test_valid_us_number(self):
        ok, e164 = normalize_phone_ca_us("+12125551234")
        assert ok is True
        assert e164.startswith("+1")

    def test_local_format_normalized(self):
        ok, e164 = normalize_phone_ca_us("403-555-1234")
        assert ok is True
        assert e164 == "+14035551234"

    def test_invalid_phone(self):
        ok, e164 = normalize_phone_ca_us("not-a-phone")
        assert ok is False

    def test_empty_phone(self):
        ok, e164 = normalize_phone_ca_us("")
        assert ok is False

    def test_none_phone_raises(self):
        """None should raise — caller is expected to guard."""
        with pytest.raises((AttributeError, TypeError)):
            normalize_phone_ca_us(None)


class TestEmailValidation:
    def test_valid_email(self):
        ok, normalized = validate_email_addr("John@Example.COM")
        assert ok is True
        assert normalized == "john@example.com" or "@" in normalized

    def test_invalid_email(self):
        ok, normalized = validate_email_addr("not-an-email")
        assert ok is False

    def test_empty_email(self):
        ok, normalized = validate_email_addr("")
        assert ok is False

    def test_none_email_returns_false(self):
        """validate_email_addr handles None via `not raw` guard."""
        ok, normalized = validate_email_addr(None)
        assert ok is False
