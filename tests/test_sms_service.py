"""Tests for SMS service — outbound send, provider routing, hardening guards."""

import pytest
from unittest.mock import MagicMock, patch

from app.sms.service import (
    SmsProvider,
    SmsService,
    _reset_lookup_cache,
    _reset_rate_limit_store,
)


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Each test gets a fresh rate-limit store + Lookup cache."""
    _reset_rate_limit_store()
    _reset_lookup_cache()
    yield
    _reset_rate_limit_store()
    _reset_lookup_cache()


class TestSmsService:
    def _make_service(
        self,
        twilio_configured=True,
        *,
        rate_limit_enabled: bool = False,
        rate_limit_per_24h: int = 20,
        lookup_enabled: bool = False,
        lookup_allowed_types: str = "mobile",
        lookup_line_type: str = "mobile",
        lookup_raises: bool = False,
    ):
        with patch("app.sms.service.get_settings") as mock_s:
            s = MagicMock()
            if twilio_configured:
                s.twilio_account_sid = "AC_test"
                s.twilio_auth_token = "token_test"
                s.twilio_phone_number = "+14035550000"
            else:
                s.twilio_account_sid = ""
                s.twilio_auth_token = ""
                s.twilio_phone_number = ""
            s.sms_rate_limit_enabled = rate_limit_enabled
            s.sms_rate_limit_per_24h = rate_limit_per_24h
            s.sms_lookup_enabled = lookup_enabled
            s.sms_lookup_allowed_types = lookup_allowed_types
            s.sms_lookup_cache_ttl_seconds = 86400
            mock_s.return_value = s
            with patch("app.sms.service.TwilioClient") as mock_tc:
                mock_client = MagicMock()
                mock_msg = MagicMock()
                mock_msg.sid = "SM_test123"
                mock_client.messages.create.return_value = mock_msg
                # Wire the Lookup v2 chain:
                # client.lookups.v2.phone_numbers(num).fetch(fields=...)
                if lookup_raises:
                    mock_client.lookups.v2.phone_numbers.return_value.fetch.side_effect = (
                        RuntimeError("twilio lookup outage")
                    )
                else:
                    mock_phone = MagicMock()
                    mock_phone.line_type_intelligence = {"type": lookup_line_type}
                    mock_client.lookups.v2.phone_numbers.return_value.fetch.return_value = (
                        mock_phone
                    )
                mock_tc.return_value = mock_client
                svc = SmsService()
                svc._mock_client = mock_client  # stash for assertions
                return svc

    @pytest.mark.asyncio
    async def test_send_via_twilio(self):
        svc = self._make_service(twilio_configured=True)
        sid = await svc.send("+14035551234", "Hello from Sarah")
        assert sid == "SM_test123"
        svc._mock_client.messages.create.assert_called_once()
        call_kwargs = svc._mock_client.messages.create.call_args
        assert call_kwargs.kwargs["to"] == "+14035551234"

    @pytest.mark.asyncio
    async def test_send_truncates_long_messages(self):
        svc = self._make_service(twilio_configured=True)
        long_msg = "x" * 2000
        await svc.send("+14035551234", long_msg)
        call_kwargs = svc._mock_client.messages.create.call_args
        assert len(call_kwargs.kwargs["body"]) <= 1600

    @pytest.mark.asyncio
    async def test_send_without_twilio_returns_none(self):
        svc = self._make_service(twilio_configured=False)
        result = await svc.send("+14035551234", "Hello")
        assert result is None

    @pytest.mark.asyncio
    async def test_ghl_lead_connector_falls_back_to_twilio(self):
        svc = self._make_service(twilio_configured=True)
        sid = await svc.send(
            "+14035551234", "Hello",
            provider=SmsProvider.GHL_LEAD_CONNECTOR,
        )
        assert sid == "SM_test123"

    def test_provider_enum_values(self):
        assert SmsProvider.TWILIO == "twilio"
        assert SmsProvider.GHL_LEAD_CONNECTOR == "ghl_lead_connector"


class TestSmsRateLimit:
    """B-soft.1 — per-recipient daily SMS rate limit."""

    def _svc(self, **kw):
        return TestSmsService()._make_service(**kw)

    @pytest.mark.asyncio
    async def test_disabled_by_default_allows_unlimited(self):
        svc = self._svc(rate_limit_enabled=False, rate_limit_per_24h=2)
        for _ in range(5):
            sid = await svc.send("+14035551111", "hi")
            assert sid == "SM_test123"
        assert svc._mock_client.messages.create.call_count == 5

    @pytest.mark.asyncio
    async def test_enabled_blocks_after_limit(self):
        svc = self._svc(rate_limit_enabled=True, rate_limit_per_24h=3)
        for _ in range(3):
            assert await svc.send("+14035551111", "hi") == "SM_test123"
        # 4th send: blocked
        assert await svc.send("+14035551111", "hi") is None
        assert svc._mock_client.messages.create.call_count == 3

    @pytest.mark.asyncio
    async def test_limit_is_per_recipient(self):
        svc = self._svc(rate_limit_enabled=True, rate_limit_per_24h=1)
        assert await svc.send("+14035551111", "hi") == "SM_test123"
        # Different recipient gets its own bucket
        assert await svc.send("+14035552222", "hi") == "SM_test123"
        # First recipient still blocked
        assert await svc.send("+14035551111", "hi") is None
        assert svc._mock_client.messages.create.call_count == 2

    @pytest.mark.asyncio
    async def test_window_eviction(self):
        """Entries older than 24h drop out of the sliding window."""
        from app.sms import service as svc_mod

        svc = self._svc(rate_limit_enabled=True, rate_limit_per_24h=2)
        # Manually backfill the deque with stale timestamps (>24h ago)
        stale = svc_mod.time.time() - svc_mod._RATE_LIMIT_WINDOW_SECONDS - 100
        svc_mod._rate_limit_log["+14035551111"].extend([stale, stale])
        # Should still allow new sends because stale entries get evicted
        assert await svc.send("+14035551111", "hi") == "SM_test123"
        assert await svc.send("+14035551111", "hi") == "SM_test123"
        # 3rd should now be blocked (2 fresh entries fill the window)
        assert await svc.send("+14035551111", "hi") is None

    @pytest.mark.asyncio
    async def test_no_record_when_twilio_unconfigured(self):
        """If Twilio isn't configured we return None BEFORE consuming a slot."""
        from app.sms import service as svc_mod

        svc = self._svc(twilio_configured=False, rate_limit_enabled=True, rate_limit_per_24h=1)
        assert await svc.send("+14035551111", "hi") is None
        assert "+14035551111" not in svc_mod._rate_limit_log


class TestSmsLookupPreflight:
    """B-soft.2 — Twilio Lookup pre-flight."""

    def _svc(self, **kw):
        return TestSmsService()._make_service(**kw)

    @pytest.mark.asyncio
    async def test_disabled_by_default_skips_lookup(self):
        svc = self._svc(lookup_enabled=False)
        sid = await svc.send("+14035551234", "hi")
        assert sid == "SM_test123"
        # Lookup chain not exercised
        svc._mock_client.lookups.v2.phone_numbers.assert_not_called()

    @pytest.mark.asyncio
    async def test_mobile_allowed_passes_through(self):
        svc = self._svc(
            lookup_enabled=True,
            lookup_allowed_types="mobile",
            lookup_line_type="mobile",
        )
        sid = await svc.send("+14035551234", "hi")
        assert sid == "SM_test123"
        svc._mock_client.lookups.v2.phone_numbers.assert_called_once_with("+14035551234")
        svc._mock_client.messages.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_landline_rejected(self):
        svc = self._svc(
            lookup_enabled=True,
            lookup_allowed_types="mobile",
            lookup_line_type="landline",
        )
        result = await svc.send("+14035551234", "hi")
        assert result is None
        svc._mock_client.messages.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_voip_rejected_when_only_mobile_allowed(self):
        svc = self._svc(
            lookup_enabled=True,
            lookup_allowed_types="mobile",
            lookup_line_type="nonFixedVoip",
        )
        result = await svc.send("+14035551234", "hi")
        assert result is None
        svc._mock_client.messages.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_multi_allowed_types_csv(self):
        svc = self._svc(
            lookup_enabled=True,
            lookup_allowed_types="mobile, nonFixedVoip",
            lookup_line_type="nonfixedvoip",  # case-insensitive
        )
        sid = await svc.send("+14035551234", "hi")
        assert sid == "SM_test123"

    @pytest.mark.asyncio
    async def test_lookup_failure_fails_open(self):
        """When Twilio Lookup itself errors, we MUST proceed with the send."""
        svc = self._svc(
            lookup_enabled=True,
            lookup_allowed_types="mobile",
            lookup_raises=True,
        )
        sid = await svc.send("+14035551234", "hi")
        assert sid == "SM_test123"
        svc._mock_client.messages.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_lookup_result_cached(self):
        """Second send to the same number should not re-hit Lookup."""
        svc = self._svc(
            lookup_enabled=True,
            lookup_allowed_types="mobile",
            lookup_line_type="mobile",
        )
        await svc.send("+14035551234", "hi")
        await svc.send("+14035551234", "hi again")
        # Lookup called once, but messages sent twice
        assert svc._mock_client.lookups.v2.phone_numbers.call_count == 1
        assert svc._mock_client.messages.create.call_count == 2

    @pytest.mark.asyncio
    async def test_rate_limit_runs_before_lookup(self):
        """Rate-limit-blocked sends must not pay the Lookup API cost."""
        svc = self._svc(
            rate_limit_enabled=True,
            rate_limit_per_24h=1,
            lookup_enabled=True,
            lookup_allowed_types="mobile",
            lookup_line_type="mobile",
        )
        await svc.send("+14035551234", "hi")  # consumed
        result = await svc.send("+14035551234", "hi")  # rate-limited
        assert result is None
        # Lookup hit only on the first (allowed) call
        assert svc._mock_client.lookups.v2.phone_numbers.call_count == 1
