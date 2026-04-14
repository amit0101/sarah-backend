"""Base HTTP client: auth, rate limiting, retries, errors."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# Global throttle across all GHL clients (per revision: document as shared limiter).
_global_lock = asyncio.Lock()
_global_window_start = time.monotonic()
_global_count = 0
_MAX_PER_SECOND_GLOBAL = 36


class GHLAPIError(Exception):
    """Raised when GHL returns an error body or non-success status."""

    def __init__(self, message: str, status_code: int = 0, body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class GHLClient:
    """
    Async HTTP client for one GHL sub-account (API key + default Location-Id).
    Rate limiting: process-wide sliding window shared by all instances.
    """

    def __init__(
        self,
        *,
        api_key: str,
        default_location_id: str,
        api_base_url: Optional[str] = None,
        api_version: Optional[str] = None,
    ) -> None:
        s = get_settings()
        self._base = (api_base_url or s.ghl_api_base_url).rstrip("/")
        self._version = api_version or s.ghl_api_version
        self._token = api_key
        self._default_location = default_location_id
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))

    def _headers(self, location_id: Optional[str] = None) -> Dict[str, str]:
        loc = location_id or self._default_location
        h: Dict[str, str] = {
            "Authorization": f"Bearer {self._token}",
            "Version": self._version,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if loc:
            h["Location-Id"] = loc
        return h

    async def _throttle_global(self) -> None:
        global _global_window_start, _global_count
        async with _global_lock:
            now = time.monotonic()
            if now - _global_window_start >= 1.0:
                _global_window_start = now
                _global_count = 0
            if _global_count >= _MAX_PER_SECOND_GLOBAL:
                wait = 1.0 - (now - _global_window_start)
                if wait > 0:
                    await asyncio.sleep(wait)
                _global_window_start = time.monotonic()
                _global_count = 0
            _global_count += 1

    async def request(
        self,
        method: str,
        path: str,
        *,
        location_id: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
    ) -> Any:
        url = f"{self._base}{path}" if path.startswith("/") else f"{self._base}/{path}"
        last_error: Optional[Exception] = None
        for attempt in range(5):
            await self._throttle_global()
            resp = await self._client.request(
                method,
                url,
                headers=self._headers(location_id),
                params=params,
                json=json_body,
            )
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "2"))
                await asyncio.sleep(min(retry_after, 30))
                last_error = GHLAPIError("Rate limited", status_code=429)
                continue
            if resp.status_code >= 500:
                last_error = httpx.HTTPStatusError(
                    "Server error", request=resp.request, response=resp
                )
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                raise GHLAPIError(
                    f"GHL API error {resp.status_code}",
                    status_code=resp.status_code,
                    body=body,
                )
            if resp.content:
                try:
                    return resp.json()
                except Exception:
                    return resp.text
            return None
        if last_error:
            raise last_error
        raise GHLAPIError("GHL request failed after retries", status_code=0)

    async def aclose(self) -> None:
        await self._client.aclose()
