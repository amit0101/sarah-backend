"""HTTP client for Tribute Center Online (TCO) — public obituary search.

TCO is the platform powering www.mhfh.com/obituaries (and ~all Tribute
Technology funeral-home sites). Read endpoints are anonymous; the only
auth is the `DomainId` request header which scopes results to the
funeral home. The path was reverse-engineered from the website's Vue
bundle (`chunk-store.4f6fd695.js`):

    GET ${apiBaseUrl}/obituaries/GetObituariesExtended
        headers: { DomainId: <uuid> }
        params:  { pageNumber, pageSize, searchTerm,
                   sortingColumn, servingLocationId }

Sample response (truncated):

    [{ "Id": 48139237, "FirstName": "David", "MiddleName": "Alan",
       "LastName": "Seal", "FullName": "David Seal",
       "BirthDate": "1963-10-24T00:00:00",
       "DeathDate": "2026-04-09T00:00:00",
       "Description": "<p>...</p>",
       "ServingLocationName": "McInnis & Holloway, Crowfoot",
       "ThumbnailUrl": "Obituaries/48139237/Thumbnail_1.jpg",
       ... },
       ...]

`search()` returns a list of normalised dicts that Sarah's
`search_obituary` tool serialises directly to the model.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# `sortingColumn=3` mirrors the website default (most recent death date first).
_DEFAULT_SORTING_COLUMN = 3
_DEFAULT_PAGE_SIZE = 12


class TributeCenterClient:
    def __init__(self) -> None:
        s = get_settings()
        self._base = (s.tribute_center_base_url or "").rstrip("/")
        self._domain_id = (s.tribute_center_domain_id or "").strip()

    async def search(
        self,
        *,
        name: Optional[str] = None,
        date: Optional[str] = None,                # accepted for API compat; not sent to TCO
        location_hint: Optional[str] = None,       # DEPRECATED: accepted but ignored (see docstring)
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> List[Dict[str, Any]]:
        """Search recent obituaries by name; anonymous (DomainId header only).

        Args:
          name:           free-text search (matched against first/middle/last).
                          Empty / None ⇒ recent obituaries (sorted by death date).
          date:           ignored — TCO's GetObituariesExtended has no server-side
                          date filter. Included so older callers keep working.
          location_hint:  DEPRECATED. Accepted-but-ignored. Visitors generally
                          don't know which chapel served their loved one — that's
                          what they're asking the search to surface — and the
                          `ServingLocationName` field holds chapel names not
                          cities, so substring filtering produces wrong results.
                          Keep the parameter for backward compat; never filter.
          page_size:      number of records to ask TCO for (default 12).
        """
        if not self._base or not self._domain_id:
            logger.warning(
                "Tribute client not configured (base=%s domain_id=%s) — empty results",
                bool(self._base), bool(self._domain_id),
            )
            return []

        params = {
            "pageNumber": 1,
            "pageSize": page_size,
            "searchTerm": (name or "").strip(),
            "sortingColumn": _DEFAULT_SORTING_COLUMN,
            "servingLocationId": 0,
        }
        headers = {
            "DomainId": self._domain_id,
            "Accept": "application/json",
        }
        url = f"{self._base}/obituaries/GetObituariesExtended"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(url, params=params, headers=headers)
                r.raise_for_status()
                data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("tribute_search_failed name=%r err=%s", name, exc)
            return []

        if not isinstance(data, list):
            logger.warning("tribute_search_unexpected_shape type=%s", type(data).__name__)
            return []

        results = [self._normalise(rec) for rec in data if isinstance(rec, dict)]
        return results

    @staticmethod
    def _normalise(rec: Dict[str, Any]) -> Dict[str, Any]:
        """Project a TCO record into a Sarah-friendly stable schema."""
        full_name = (
            rec.get("FullName")
            or " ".join(
                p for p in (
                    rec.get("FirstName"),
                    rec.get("MiddleName"),
                    rec.get("LastName"),
                ) if p
            ).strip()
        )
        return {
            "id": rec.get("Id"),
            "name": (full_name or "").strip(),
            "first_name": (rec.get("FirstName") or "").strip(),
            "last_name": (rec.get("LastName") or "").strip(),
            "birth_date": _to_date(rec.get("BirthDate")),
            "death_date": _to_date(rec.get("DeathDate")),
            "location": rec.get("ServingLocationName"),
            "summary": _strip_html(rec.get("Description") or "")[:600] or None,
            "thumbnail": _absolute_thumbnail(rec.get("ThumbnailUrl")),
        }


# ─── helpers ──────────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TCO_CDN = "https://d1q40j6jx1d8h6.cloudfront.net/"


def _strip_html(html: str) -> str:
    """Cheap, dependency-free HTML → plain text. Sufficient for obituary
    summaries (Sarah will rephrase anyway)."""
    if not html:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()


def _to_date(iso: Optional[str]) -> Optional[str]:
    """Take TCO's '1963-10-24T00:00:00' and return 'YYYY-MM-DD'."""
    if not iso or not isinstance(iso, str):
        return None
    return iso.split("T", 1)[0]


def _absolute_thumbnail(rel: Optional[str]) -> Optional[str]:
    """TCO returns thumbnails as relative paths e.g. 'Obituaries/123/Thumbnail_1.jpg'.
    Resolve against the public CDN."""
    if not rel:
        return None
    if rel.startswith("http"):
        return rel
    return _TCO_CDN + rel.lstrip("/")
