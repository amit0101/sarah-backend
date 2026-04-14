"""Tests for GHL client — API calls, retries, tag/pipeline helpers."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.ghl_client import contacts as ghl_contacts
from app.ghl_client import tags as ghl_tags
from app.ghl_client import pipelines as ghl_pipes
from app.ghl_client import calendars as ghl_cals


@pytest.fixture
def ghl():
    """Create a mock GHLClient without calling real constructor."""
    client = MagicMock()
    client.request = AsyncMock(return_value={"ok": True})
    client._default_location = "loc-123"
    return client


class TestGHLContacts:
    @pytest.mark.asyncio
    async def test_create_contact(self, ghl):
        ghl.request = AsyncMock(return_value={"contact": {"id": "c-new-123"}})
        result = await ghl_contacts.create_contact(
            ghl, location_id="loc-123",
            name="Jane Doe", phone="+14035551234",
        )
        assert result["contact"]["id"] == "c-new-123"
        ghl.request.assert_called_once()

    @pytest.mark.asyncio
    async def test_lookup_contact_by_phone(self, ghl):
        ghl.request = AsyncMock(return_value={"contact": {"id": "c-found", "phone": "+1234"}})
        result = await ghl_contacts.lookup_contact(ghl, location_id="loc-123", phone="+1234")
        assert result is not None
        assert result["id"] == "c-found"

    @pytest.mark.asyncio
    async def test_lookup_contact_not_found(self, ghl):
        from app.ghl_client.client import GHLAPIError
        ghl.request = AsyncMock(side_effect=GHLAPIError("Not found", status_code=404))
        result = await ghl_contacts.lookup_contact(ghl, location_id="loc-123", phone="+9999")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_contact(self, ghl):
        ghl.request = AsyncMock(return_value={"contact": {"id": "c-123"}})
        await ghl_contacts.update_contact(
            ghl, "c-123", location_id="loc-123", name="Updated Name"
        )
        ghl.request.assert_called_once()


class TestGHLTags:
    @pytest.mark.asyncio
    async def test_add_tags(self, ghl):
        ghl.request = AsyncMock(return_value={"tags": ["webchat_lead"]})
        await ghl_tags.add_tags(ghl, "c-123", location_id="loc-123", tags=["webchat_lead"])
        ghl.request.assert_called_once()

    @pytest.mark.asyncio
    async def test_remove_tags(self, ghl):
        ghl.request = AsyncMock(return_value={"tags": []})
        await ghl_tags.remove_tags(ghl, "c-123", location_id="loc-123", tag_ids=["old_tag"])
        ghl.request.assert_called_once()


class TestGHLPipelines:
    @pytest.mark.asyncio
    async def test_create_opportunity(self, ghl):
        ghl.request = AsyncMock(return_value={"opportunity": {"id": "opp-123"}})
        result = await ghl_pipes.create_opportunity(
            ghl, location_id="loc-123",
            contact_id="c-123", pipeline_id="pipe-1", pipeline_stage_id="stage-1",
        )
        assert result["opportunity"]["id"] == "opp-123"


class TestGHLCalendars:
    @pytest.mark.asyncio
    async def test_create_appointment(self, ghl):
        ghl.request = AsyncMock(return_value={"id": "apt-123"})
        result = await ghl_cals.create_appointment(
            ghl, calendar_id="cal-1", location_id="loc-123",
            contact_id="c-123", start_time="2026-04-10T10:00:00",
            end_time="2026-04-10T11:00:00", title="Pre-Need Consultation",
        )
        ghl.request.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_appointment(self, ghl):
        ghl.request = AsyncMock(return_value={})
        await ghl_cals.cancel_appointment(ghl, "apt-123", location_id="loc-123")
        ghl.request.assert_called_once()
