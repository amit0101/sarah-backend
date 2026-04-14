"""SMS (Twilio) + email alerts — Section 4.9.

Accepts structured escalation payloads and formats notifications
with contact name, conversation context, and actionable information.
"""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Dict, Optional

from twilio.rest import Client as TwilioClient

from app.config import get_settings

logger = logging.getLogger(__name__)


def _business_hours_mdt(now: Optional[datetime] = None) -> bool:
    """Approximate M&H business hours Mon–Fri 8–5 Mountain (simplified)."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Edmonton")
    t = now or datetime.now(tz)
    if t.weekday() >= 5:
        return False
    return 8 <= t.hour < 17


class NotificationService:
    def __init__(self) -> None:
        s = get_settings()
        self._twilio = (
            TwilioClient(s.twilio_account_sid, s.twilio_auth_token)
            if s.twilio_account_sid and s.twilio_auth_token
            else None
        )
        self._from_num = s.twilio_phone_number

    async def notify_escalation(
        self,
        *,
        to_phone: Optional[str],
        to_email: Optional[str],
        body: str,
        prefer_sms_if_business_hours: bool = True,
        # Structured payload fields (optional — if provided, build formatted message)
        contact_name: Optional[str] = None,
        contact_phone: Optional[str] = None,
        location_name: Optional[str] = None,
        conversation_id: Optional[str] = None,
        reason: Optional[str] = None,
        urgency: str = "normal",
    ) -> None:
        # If structured fields provided, build a formatted message
        if reason or contact_name:
            body = self._format_escalation(
                body=body,
                contact_name=contact_name,
                contact_phone=contact_phone,
                location_name=location_name,
                conversation_id=conversation_id,
                reason=reason,
                urgency=urgency,
            )

        if prefer_sms_if_business_hours and _business_hours_mdt() and to_phone and self._twilio and self._from_num:
            self._twilio.messages.create(to=to_phone, from_=self._from_num, body=body[:1600])
            logger.info("Sent escalation SMS to %s", to_phone)
            return
        if to_email:
            subject = f"Sarah Escalation ({urgency})" + (f" — {location_name}" if location_name else "")
            self._send_smtp_email(to_email, subject, body)
            logger.info("Sent escalation email to %s", to_email)
            return
        if to_phone and self._twilio and self._from_num:
            self._twilio.messages.create(to=to_phone, from_=self._from_num, body=body[:1600])
            logger.info("Sent escalation SMS (fallback) to %s", to_phone)

    async def notify_hot_lead(
        self,
        *,
        to_phone: Optional[str],
        to_email: Optional[str],
        contact_name: Optional[str] = None,
        contact_phone: Optional[str] = None,
        location_name: Optional[str] = None,
        conversation_id: Optional[str] = None,
        lead_type: str = "pre_need",
    ) -> None:
        """GHL_INTEGRATION_DOC Section 10, Workflow 5 — hot lead notification.

        Fires when a contact shows strong buying intent (e.g., requests
        appointment, asks about pricing, mentions timeline).
        """
        body = (
            f"🔥 HOT LEAD at {location_name or 'Unknown location'}\n"
            f"Contact: {contact_name or 'Unknown'}\n"
            f"Phone: {contact_phone or 'Not provided'}\n"
            f"Type: {lead_type}\n"
            f"Conversation: {conversation_id or 'N/A'}\n"
            f"Action: Follow up immediately — this contact is ready to schedule."
        )
        if to_phone and self._twilio and self._from_num:
            self._twilio.messages.create(to=to_phone, from_=self._from_num, body=body[:1600])
            logger.info("Sent hot lead SMS to %s", to_phone)
        if to_email:
            self._send_smtp_email(to_email, f"🔥 Hot Lead — {contact_name or 'New Contact'}", body)
            logger.info("Sent hot lead email to %s", to_email)

    def _format_escalation(
        self,
        *,
        body: str,
        contact_name: Optional[str],
        contact_phone: Optional[str],
        location_name: Optional[str],
        conversation_id: Optional[str],
        reason: Optional[str],
        urgency: str,
    ) -> str:
        """Format a structured escalation message — Section 4.9."""
        urgency_label = "🚨 URGENT" if urgency == "high" else "ℹ️ Normal"
        parts = [
            f"{urgency_label} — Sarah Escalation",
            "",
        ]
        if location_name:
            parts.append(f"Location: {location_name}")
        if contact_name:
            parts.append(f"Contact: {contact_name}")
        if contact_phone:
            parts.append(f"Phone: {contact_phone}")
        if reason:
            parts.append(f"Reason: {reason}")
        if conversation_id:
            parts.append(f"Conversation ID: {conversation_id}")
        parts.append("")
        parts.append("Please follow up with this contact.")
        return "\n".join(parts)

    def _send_smtp_email(self, to_addr: str, subject: str, body: str) -> None:
        """Optional SMTP via env — if not configured, log only."""
        import os

        host = os.getenv("SMTP_HOST", "")
        if not host:
            logger.warning("SMTP not configured; email not sent: %s", subject)
            return
        port = int(os.getenv("SMTP_PORT", "587"))
        user = os.getenv("SMTP_USER", "")
        password = os.getenv("SMTP_PASSWORD", "")
        from_addr = os.getenv("SMTP_FROM", user)
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg.set_content(body)
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
