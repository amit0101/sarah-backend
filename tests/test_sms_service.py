"""Tests for SMS service — outbound send, provider routing."""

import pytest
from unittest.mock import MagicMock, patch

from app.sms.service import SmsService, SmsProvider


class TestSmsService:
    def _make_service(self, twilio_configured=True):
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
            mock_s.return_value = s
            with patch("app.sms.service.TwilioClient") as mock_tc:
                mock_client = MagicMock()
                mock_msg = MagicMock()
                mock_msg.sid = "SM_test123"
                mock_client.messages.create.return_value = mock_msg
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
