"""HTTP client for Tribute Center API (configurable base URL)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class TributeCenterClient:
    def __init__(self) -> None:
        s = get_settings()
        self._base = (s.tribute_center_base_url or "").rstrip("/")
        self._key = s.tribute_center_api_key

    async def search(
        self,
        *,
        name: Optional[str] = None,
        date: Optional[str] = None,
        location_hint: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not self._base:
            logger.warning("TRIBUTE_CENTER_BASE_URL not configured — returning empty results")
            return []
        params: Dict[str, Any] = {}
        if name:
            params["name"] = name
        if date:
            params["date"] = date
        if location_hint:
            params["location"] = location_hint
        headers = {}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(f"{self._base}/search", params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "results" in data:
                return list(data["results"])
            return [data] if data else []
