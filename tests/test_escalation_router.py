"""Tests for escalation router — Section 4.9."""

import pytest
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo

from app.escalation.router import EscalationRouter, EscalationResult


@pytest.fixture
def router():
    return EscalationRouter()


@pytest.fixture
def contacts():
    return [
        {"name": "Jane", "role": "director", "phone": "+14035551234", "email": "jane@test.com"},
        {"name": "Bob", "role": "staff", "phone": "+14035555678"},
        {"name": "Alice", "role": "manager", "phone": "+14035559000", "email": "alice@test.com"},
    ]


@pytest.fixture
def config():
    return {
        "business_hours": {
            "mon": {"open": "08:00", "close": "17:00"},
            "tue": {"open": "08:00", "close": "17:00"},
            "wed": {"open": "08:00", "close": "17:00"},
            "thu": {"open": "08:00", "close": "17:00"},
            "fri": {"open": "08:00", "close": "17:00"},
        },
        "timezone": "America/Edmonton",
    }


class TestContactPicking:
    def test_prefers_director_manager(self, router, contacts):
        picked = set()
        for _ in range(50):
            c = router.pick_contact(contacts)
            picked.add(c["name"])
        # Should only pick director/manager, not staff
        assert "Bob" not in picked
        assert len(picked & {"Jane", "Alice"}) > 0

    def test_empty_contacts_returns_none(self, router):
        assert router.pick_contact(None) is None
        assert router.pick_contact([]) is None

    def test_falls_back_to_any_contact_if_no_primary(self, router):
        contacts = [{"name": "Only Staff", "role": "staff", "phone": "+1234"}]
        c = router.pick_contact(contacts)
        assert c["name"] == "Only Staff"


class TestChannelSelection:
    def _mock_business_hours(self, router, is_bh: bool):
        """Patch _is_business_hours on the instance."""
        router._is_business_hours = lambda config: is_bh

    def test_high_urgency_with_phone_gets_sms(self, router, contacts, config):
        self._mock_business_hours(router, True)
        result = router.route(contacts, urgency="high", location_config=config)
        assert result.channel == "sms"

    def test_high_urgency_after_hours_still_sms(self, router, contacts, config):
        self._mock_business_hours(router, False)
        result = router.route(contacts, urgency="high", location_config=config)
        assert result.channel == "sms"

    def test_normal_urgency_prefers_email(self, router, contacts, config):
        result = router.route(contacts, urgency="normal", location_config=config)
        assert result.channel == "email"

    def test_normal_no_email_falls_back_to_sms(self, router):
        contacts = [{"name": "Bob", "role": "staff", "phone": "+1234"}]
        result = router.route(contacts, urgency="normal")
        assert result.channel == "sms"

    def test_no_contacts_returns_email(self, router):
        result = router.route(None, urgency="high")
        assert result.channel == "email"
        assert result.contact is None


class TestBusinessHours:
    def test_weekday_during_hours(self, router, config):
        # Mock: Wednesday at 10am Mountain
        tz = ZoneInfo("America/Edmonton")
        mock_now = datetime(2026, 4, 1, 10, 0, tzinfo=tz)  # Wednesday
        with patch("app.escalation.router.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert router._is_business_hours(config) is True

    def test_weekend_outside_hours(self, router, config):
        tz = ZoneInfo("America/Edmonton")
        mock_now = datetime(2026, 4, 4, 10, 0, tzinfo=tz)  # Saturday
        with patch("app.escalation.router.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert router._is_business_hours(config) is False

    def test_no_config_uses_default(self, router):
        # Should not crash
        result = router._is_business_hours(None)
        assert isinstance(result, bool)
