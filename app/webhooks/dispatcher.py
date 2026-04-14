"""Fire-and-forget POST to Comms Platform — Section 6.1 / 10.

Retry logic: 3 retries with exponential backoff (1s, 2s, 4s).
Failed events are logged for manual replay.
Does NOT block the main conversation flow.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# URLs that failed DNS / connect; skip further emits this process (avoids log spam).
_disabled_comms_urls: Set[str] = set()

# Section 6.1 — retry configuration
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 1.0  # 1s, 2s, 4s


def _comms_url_unreachable(exc: BaseException) -> bool:
    """True when host cannot be resolved or similar (placeholder COMMS URL in .env)."""
    chain: list[BaseException] = [exc]
    c = exc.__cause__
    while c is not None and len(chain) < 5:
        chain.append(c)
        c = c.__cause__
    for e in chain:
        if isinstance(e, OSError):
            # macOS often errno 8 for "nodename nor servname provided"
            if getattr(e, "errno", None) in (8, -2, 11001):
                return True
        msg = str(e).lower()
        if any(
            x in msg
            for x in (
                "nodename nor servname",
                "name or service not known",
                "getaddrinfo failed",
                "failed to resolve",
            )
        ):
            return True
    if isinstance(exc, httpx.ConnectError):
        if exc.__cause__ is not None:
            return _comms_url_unreachable(exc.__cause__)
        msg = str(exc).lower()
        return any(
            x in msg
            for x in (
                "nodename nor servname",
                "name or service not known",
                "getaddrinfo failed",
                "failed to resolve",
            )
        )
    return False


class WebhookDispatcher:
    def __init__(self) -> None:
        s = get_settings()
        self._url = (s.comms_platform_webhook_url or "").strip()
        self._secret = s.comms_webhook_secret or ""

    async def emit(
        self,
        event: str,
        data: Dict[str, Any],
    ) -> None:
        if not self._url:
            logger.debug("No COMMS_PLATFORM_WEBHOOK_URL — skip %s", event)
            return
        if self._url in _disabled_comms_urls:
            return

        payload = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "sarah_version": get_settings().sarah_version,
            "data": data,
        }
        raw = json.dumps(payload).encode()

        headers = {"Content-Type": "application/json"}
        if self._secret:
            sig = hmac.new(self._secret.encode(), raw, hashlib.sha256).hexdigest()
            headers["X-Sarah-Signature"] = sig

        url = self._url

        async def _send_with_retry() -> None:
            """Section 6.1 — 3 retries with exponential backoff, fire-and-forget."""
            last_error: Optional[Exception] = None
            for attempt in range(_MAX_RETRIES + 1):  # 0, 1, 2, 3
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        r = await client.post(url, content=raw, headers=headers)
                        r.raise_for_status()
                        return  # Success
                except Exception as e:
                    last_error = e
                    if _comms_url_unreachable(e):
                        if url not in _disabled_comms_urls:
                            _disabled_comms_urls.add(url)
                            logger.info(
                                "Comms webhook host unreachable; skipping further emits "
                                "until restart. Set COMMS_PLATFORM_WEBHOOK_URL to a real "
                                "URL or leave it empty. (%s)",
                                e,
                            )
                        return  # Don't retry DNS failures

                    if attempt < _MAX_RETRIES:
                        backoff = _BACKOFF_BASE_SECONDS * (2 ** attempt)  # 1s, 2s, 4s
                        logger.debug(
                            "Webhook %s attempt %d failed, retrying in %.1fs: %s",
                            event,
                            attempt + 1,
                            backoff,
                            e,
                        )
                        await asyncio.sleep(backoff)

            # All retries exhausted — log for manual replay
            logger.error(
                "Webhook delivery FAILED after %d retries — event=%s url=%s error=%s "
                "payload=%s",
                _MAX_RETRIES,
                event,
                url,
                last_error,
                json.dumps(payload)[:500],  # Truncate for log readability
            )

        asyncio.create_task(_send_with_retry())
