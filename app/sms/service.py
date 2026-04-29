"""Twilio outbound; provider flag for GHL Lead Connector path.

Hardening (session 16):
- B-soft.1: per-recipient outbound rate limit (in-memory sliding window).
- B-soft.2: Twilio Lookup v2 pre-flight (carrier line-type validation).

Both are env-flag-gated and disabled by default. See `app/config.py` for the
flags and `tests/test_sms_service.py` for behavioural coverage.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from enum import Enum
from threading import Lock
from typing import Deque, Dict, Optional, Tuple

from twilio.rest import Client as TwilioClient

from app.config import get_settings

logger = logging.getLogger(__name__)


class SmsProvider(str, Enum):
    TWILIO = "twilio"
    GHL_LEAD_CONNECTOR = "ghl_lead_connector"


# --- B-soft.1: in-memory rate-limit store ------------------------------------
# Module-level so that all SmsService() instances within a process share the
# same window (each call site instantiates a fresh service today). Per-process;
# Sarah backend is single-instance on Render. If we ever scale to multi-worker,
# this needs to move to Redis/Postgres — see SESSION_HANDOFF for context.
_RATE_LIMIT_WINDOW_SECONDS = 24 * 60 * 60
_rate_limit_lock = Lock()
_rate_limit_log: Dict[str, Deque[float]] = defaultdict(deque)


def _rate_limit_check_and_record(to_e164: str, limit: int, *, now: Optional[float] = None) -> bool:
    """Return True if the send may proceed (and records it); False if rate-limited.

    Uses a per-destination deque of unix timestamps; trims entries older than
    the 24h window before each check.
    """
    if now is None:
        now = time.time()
    cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
    with _rate_limit_lock:
        dq = _rate_limit_log[to_e164]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


def _reset_rate_limit_store() -> None:
    """Test helper — clears the module-level rate-limit log."""
    with _rate_limit_lock:
        _rate_limit_log.clear()


# --- B-soft.2: in-memory Lookup result cache ---------------------------------
# (line_type, expires_at). line_type is the Twilio v2 line_type_intelligence
# `type` field (e.g. "mobile", "landline", "fixedVoip", "nonFixedVoip"). On
# Lookup API failure we cache nothing — next send retries.
_lookup_cache_lock = Lock()
_lookup_cache: Dict[str, Tuple[str, float]] = {}


def _reset_lookup_cache() -> None:
    """Test helper — clears the module-level Lookup cache."""
    with _lookup_cache_lock:
        _lookup_cache.clear()


class SmsService:
    def __init__(self) -> None:
        s = get_settings()
        self._twilio = (
            TwilioClient(s.twilio_account_sid, s.twilio_auth_token)
            if s.twilio_account_sid and s.twilio_auth_token
            else None
        )
        self._from_num = s.twilio_phone_number
        # Cache the gating settings on the instance so tests can monkeypatch
        # without re-mocking get_settings on every call.
        self._rate_limit_enabled = bool(s.sms_rate_limit_enabled)
        self._rate_limit_per_24h = int(s.sms_rate_limit_per_24h)
        self._lookup_enabled = bool(s.sms_lookup_enabled)
        self._lookup_allowed_types = {
            t.strip().lower() for t in (s.sms_lookup_allowed_types or "").split(",") if t.strip()
        }
        self._lookup_cache_ttl_seconds = int(s.sms_lookup_cache_ttl_seconds)

    def _lookup_line_type(self, to_e164: str) -> Optional[str]:
        """Return cached or fresh Twilio Lookup line_type for a number.

        Returns None if the API call fails (so callers can fail-open). Caches
        positive results for `sms_lookup_cache_ttl_seconds`.
        """
        if not self._twilio:
            return None
        now = time.time()
        with _lookup_cache_lock:
            hit = _lookup_cache.get(to_e164)
            if hit and hit[1] > now:
                return hit[0]
        try:
            # Twilio Lookup v2: client.lookups.v2.phone_numbers(...).fetch(fields=...)
            phone = self._twilio.lookups.v2.phone_numbers(to_e164).fetch(
                fields="line_type_intelligence",
            )
            lti = getattr(phone, "line_type_intelligence", None) or {}
            line_type = (lti.get("type") if isinstance(lti, dict) else None) or ""
            line_type = str(line_type).lower()
        except Exception as e:  # noqa: BLE001
            logger.warning("Twilio Lookup failed for %s — failing open: %s", to_e164, e)
            return None
        if line_type:
            with _lookup_cache_lock:
                _lookup_cache[to_e164] = (line_type, now + self._lookup_cache_ttl_seconds)
        return line_type or None

    async def send(
        self,
        to_e164: str,
        body: str,
        *,
        provider: SmsProvider = SmsProvider.TWILIO,
    ) -> Optional[str]:
        """Send SMS; GHL Lead Connector path logs and uses Twilio if configured (pragmatic fallback).

        Hardening order (each env-flag-gated, default off):
        1. Rate limit (B-soft.1): per-recipient sliding 24h window.
        2. Lookup pre-flight (B-soft.2): reject non-allowed line types.
        3. Twilio messages.create.

        On guard rejection: WARNING log + returns None (same shape as
        Twilio-not-configured), so callers' existing None-handling applies.
        """
        if provider == SmsProvider.GHL_LEAD_CONNECTOR:
            logger.info(
                "Outbound SMS for GHL Lead Connector thread — using Twilio if configured "
                "(configure GHL native send separately for production parity)."
            )
        if not self._twilio or not self._from_num:
            logger.warning("Twilio not configured; cannot send SMS")
            return None

        # B-soft.1: rate limit guard
        if self._rate_limit_enabled:
            if not _rate_limit_check_and_record(to_e164, self._rate_limit_per_24h):
                logger.warning(
                    "SMS rate limit hit for %s (>=%d in last 24h); refusing send",
                    to_e164,
                    self._rate_limit_per_24h,
                )
                return None

        # B-soft.2: Twilio Lookup pre-flight
        if self._lookup_enabled and self._lookup_allowed_types:
            line_type = self._lookup_line_type(to_e164)
            # line_type is None when Lookup itself failed → fail-open per design.
            if line_type is not None and line_type not in self._lookup_allowed_types:
                logger.warning(
                    "SMS Lookup rejected %s — line_type=%s not in allowed=%s",
                    to_e164,
                    line_type,
                    sorted(self._lookup_allowed_types),
                )
                return None

        msg = self._twilio.messages.create(to=to_e164, from_=self._from_num, body=body[:1600])
        return msg.sid
