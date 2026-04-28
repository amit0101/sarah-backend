"""Unit tests for the SMS inbound blocklist guard.

Context: session 13 misdirected an outbound dev-test SMS to a real stranger.
This guard ensures Sarah will not engage if that number ever replies.
"""
from __future__ import annotations

from unittest.mock import patch

from app.api.routes.webhooks import _is_blocked_inbound


def _settings(blocklist: str):
    """Patch get_settings to return a Settings-like with the given blocklist."""
    from types import SimpleNamespace

    return SimpleNamespace(sms_inbound_blocklist=blocklist)


def test_empty_blocklist_blocks_nothing():
    with patch("app.api.routes.webhooks.get_settings", return_value=_settings("")):
        assert _is_blocked_inbound("+14032005678") is False


def test_single_number_blocked():
    with patch(
        "app.api.routes.webhooks.get_settings",
        return_value=_settings("+14032005678"),
    ):
        assert _is_blocked_inbound("+14032005678") is True
        assert _is_blocked_inbound("+15871234567") is False


def test_multiple_numbers_csv_blocked():
    with patch(
        "app.api.routes.webhooks.get_settings",
        return_value=_settings("+14032005678, +15871234567 ,+18005551234"),
    ):
        assert _is_blocked_inbound("+14032005678") is True
        assert _is_blocked_inbound("+15871234567") is True
        assert _is_blocked_inbound("+18005551234") is True
        assert _is_blocked_inbound("+14039998888") is False


def test_case_and_whitespace_insensitive():
    with patch(
        "app.api.routes.webhooks.get_settings",
        return_value=_settings("  +14032005678  "),
    ):
        assert _is_blocked_inbound(" +14032005678 ") is True
