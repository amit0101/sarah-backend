"""Twilio outbound; provider flag for GHL Lead Connector path."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

from twilio.rest import Client as TwilioClient

from app.config import get_settings

logger = logging.getLogger(__name__)


class SmsProvider(str, Enum):
    TWILIO = "twilio"
    GHL_LEAD_CONNECTOR = "ghl_lead_connector"


class SmsService:
    def __init__(self) -> None:
        s = get_settings()
        self._twilio = (
            TwilioClient(s.twilio_account_sid, s.twilio_auth_token)
            if s.twilio_account_sid and s.twilio_auth_token
            else None
        )
        self._from_num = s.twilio_phone_number

    async def send(
        self,
        to_e164: str,
        body: str,
        *,
        provider: SmsProvider = SmsProvider.TWILIO,
    ) -> Optional[str]:
        """Send SMS; GHL Lead Connector path logs and uses Twilio if configured (pragmatic fallback)."""
        if provider == SmsProvider.GHL_LEAD_CONNECTOR:
            logger.info(
                "Outbound SMS for GHL Lead Connector thread — using Twilio if configured "
                "(configure GHL native send separately for production parity)."
            )
        if not self._twilio or not self._from_num:
            logger.warning("Twilio not configured; cannot send SMS")
            return None
        msg = self._twilio.messages.create(to=to_e164, from_=self._from_num, body=body[:1600])
        return msg.sid
