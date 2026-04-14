"""Pick escalation contact and determine notification channel — Section 4.9.

Uses business hours from location config + urgency to decide SMS vs email.
Urgent escalations during business hours → SMS.
After-hours or normal urgency → email (fallback to SMS if no email).
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


class EscalationResult:
    """Holds the chosen contact and notification channel."""

    def __init__(
        self,
        contact: Optional[Dict[str, Any]],
        channel: str,  # "sms" | "email"
    ) -> None:
        self.contact = contact
        self.channel = channel
        self.phone = (contact or {}).get("phone")
        self.email = (contact or {}).get("email")


class EscalationRouter:
    def route(
        self,
        escalation_contacts: Optional[List[Dict[str, Any]]],
        *,
        urgency: str = "normal",
        location_config: Optional[Dict[str, Any]] = None,
    ) -> EscalationResult:
        """Pick the best escalation contact and determine notification channel.

        Args:
            escalation_contacts: List of contact dicts from location.escalation_contacts
            urgency: "normal" or "high"
            location_config: location.config JSON with optional business_hours + timezone
        """
        contact = self.pick_contact(escalation_contacts)
        channel = self._determine_channel(
            contact=contact,
            urgency=urgency,
            config=location_config,
        )
        return EscalationResult(contact=contact, channel=channel)

    def pick_contact(
        self, escalation_contacts: Optional[List[Dict[str, Any]]]
    ) -> Optional[Dict[str, Any]]:
        """Pick from escalation contacts, preferring primary/director/manager roles."""
        if not escalation_contacts:
            return None
        primary = [
            c
            for c in escalation_contacts
            if str(c.get("role", "")).lower() in ("director", "manager", "primary")
        ]
        pool = primary or escalation_contacts
        return random.choice(pool)

    def _determine_channel(
        self,
        *,
        contact: Optional[Dict[str, Any]],
        urgency: str,
        config: Optional[Dict[str, Any]],
    ) -> str:
        """Section 4.9 — SMS for urgent during business hours, email otherwise."""
        if not contact:
            return "email"

        has_phone = bool((contact or {}).get("phone"))
        has_email = bool((contact or {}).get("email"))

        # High urgency during business hours → SMS (fastest response)
        if urgency == "high" and has_phone:
            if self._is_business_hours(config):
                return "sms"
            # High urgency after hours → still SMS (it's urgent)
            return "sms"

        # Normal urgency → email preferred (less intrusive)
        if has_email:
            return "email"

        # Fallback to SMS if no email
        if has_phone:
            return "sms"

        return "email"

    def _is_business_hours(self, config: Optional[Dict[str, Any]]) -> bool:
        """Check if current time is within configured business hours."""
        if not config:
            return self._default_business_hours()

        bh = config.get("business_hours")
        tz_name = config.get("timezone", "America/Edmonton")

        if not bh:
            return self._default_business_hours(tz_name)

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("America/Edmonton")

        now = datetime.now(tz)
        day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        day_key = day_names[now.weekday()]
        day_config = bh.get(day_key)

        if not day_config:
            return False  # No config for this day = closed

        open_str = str(day_config.get("open", "08:00"))
        close_str = str(day_config.get("close", "17:00"))

        try:
            open_h, open_m = map(int, open_str.split(":"))
            close_h, close_m = map(int, close_str.split(":"))
        except ValueError:
            return self._default_business_hours(tz_name)

        current_minutes = now.hour * 60 + now.minute
        open_minutes = open_h * 60 + open_m
        close_minutes = close_h * 60 + close_m

        return open_minutes <= current_minutes < close_minutes

    def _default_business_hours(self, tz_name: str = "America/Edmonton") -> bool:
        """Default M&H business hours: Mon-Fri 8am-5pm Mountain."""
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("America/Edmonton")
        now = datetime.now(tz)
        if now.weekday() >= 5:
            return False
        return 8 <= now.hour < 17
