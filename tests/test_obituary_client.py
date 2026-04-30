"""Tests for app.obituary_client.client.TributeCenterClient.

Mocks httpx.AsyncClient.get so no network is required. Validates:
  - request shape (URL, DomainId header, params)
  - response normalisation (TCO PascalCase → Sarah snake_case)
  - location_hint parameter is accepted-but-ignored (DEPRECATED)
  - graceful fallback when not configured / on transport errors
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.obituary_client.client import TributeCenterClient


# --- Fixtures -----------------------------------------------------------------


_SAMPLE_RECORD: Dict[str, Any] = {
    "Id": 48139237,
    "FirstName": "David",
    "MiddleName": "Alan",
    "LastName": "Seal",
    "FullName": "David Seal",
    "BirthDate": "1963-10-24T00:00:00",
    "DeathDate": "2026-04-09T00:00:00",
    "Description": "<p>A kind man.</p><p>Survived by family.</p>",
    "ServingLocationName": "McInnis & Holloway, Crowfoot",
    "ThumbnailUrl": "Obituaries/48139237/Thumbnail_1.jpg",
}


def _mock_response(payload):
    resp = MagicMock(spec=httpx.Response)
    resp.json = MagicMock(return_value=payload)
    resp.raise_for_status = MagicMock()
    return resp


def _patched_settings(monkeypatch, *, base="https://api.secure.tributecenteronline.com/ClientApi",
                      domain="ee93aebe-51b2-489e-8a60-5fe98e33065b"):
    """Monkey-patch get_settings so the client picks up our test config."""
    from app import config as cfg_mod
    from app.obituary_client import client as client_mod

    settings = MagicMock()
    settings.tribute_center_base_url = base
    settings.tribute_center_domain_id = domain
    settings.tribute_center_api_key = ""
    monkeypatch.setattr(client_mod, "get_settings", lambda: settings)


# --- Tests --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_sends_correct_request_and_normalises(monkeypatch):
    _patched_settings(monkeypatch)

    captured: Dict[str, Any] = {}

    async def fake_get(self, url, *, params=None, headers=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _mock_response([_SAMPLE_RECORD])

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = TributeCenterClient()
    results: List[Dict[str, Any]] = await client.search(name="seal")

    # Request shape
    assert captured["url"] == (
        "https://api.secure.tributecenteronline.com/ClientApi/obituaries/GetObituariesExtended"
    )
    assert captured["headers"]["DomainId"] == "ee93aebe-51b2-489e-8a60-5fe98e33065b"
    assert captured["params"]["searchTerm"] == "seal"
    assert captured["params"]["pageNumber"] == 1
    assert captured["params"]["sortingColumn"] == 3
    assert captured["params"]["servingLocationId"] == 0

    # Response normalisation
    assert len(results) == 1
    r = results[0]
    assert r["id"] == 48139237
    assert r["name"] == "David Seal"
    assert r["first_name"] == "David"
    assert r["last_name"] == "Seal"
    assert r["birth_date"] == "1963-10-24"
    assert r["death_date"] == "2026-04-09"
    assert r["location"] == "McInnis & Holloway, Crowfoot"
    # Description HTML should be stripped to plain text
    assert "<p>" not in (r["summary"] or "")
    assert "kind man" in r["summary"]
    # Thumbnail resolved against TCO CDN
    assert r["thumbnail"] == (
        "https://d1q40j6jx1d8h6.cloudfront.net/"
        "Obituaries/48139237/Thumbnail_1.jpg"
    )


@pytest.mark.asyncio
async def test_location_hint_is_accepted_but_ignored(monkeypatch):
    """`location_hint` is DEPRECATED: visitors don't know which chapel served
    their loved one (that's what they're asking the search to surface), and
    `ServingLocationName` holds chapel names rather than cities, so substring
    filtering produced wrong results. The parameter is preserved for caller
    backward compat but must never filter the response."""
    _patched_settings(monkeypatch)

    rec_crowfoot = {**_SAMPLE_RECORD, "Id": 1, "ServingLocationName": "McInnis & Holloway, Crowfoot"}
    rec_park = {**_SAMPLE_RECORD, "Id": 2, "ServingLocationName": "McInnis & Holloway, Park Memorial"}

    async def fake_get(self, url, *, params=None, headers=None):
        return _mock_response([rec_crowfoot, rec_park])

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = TributeCenterClient()
    results = await client.search(name="", location_hint="Park Memorial")

    # Both records returned regardless of the hint — no client-side filtering.
    assert sorted(r["id"] for r in results) == [1, 2]


@pytest.mark.asyncio
async def test_search_returns_empty_when_unconfigured(monkeypatch):
    _patched_settings(monkeypatch, base="", domain="")
    client = TributeCenterClient()
    assert await client.search(name="anything") == []


@pytest.mark.asyncio
async def test_search_returns_empty_on_transport_error(monkeypatch):
    _patched_settings(monkeypatch)

    async def boom(self, url, *, params=None, headers=None):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx.AsyncClient, "get", boom)

    client = TributeCenterClient()
    assert await client.search(name="anyone") == []
