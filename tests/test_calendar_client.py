"""Tests for calendar client — Google adapter interface."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.calendar_client.google_adapter import GoogleCalendarAdapter


class TestCalendarClientProtocol:
    """Verify GoogleCalendarAdapter has the required methods."""

    def test_adapter_has_required_methods(self):
        with patch("app.calendar_client.google_adapter.get_settings") as ms:
            ms.return_value = MagicMock(google_calendar_credentials="", google_calendar_delegation_email=None)
            adapter = GoogleCalendarAdapter()
        assert hasattr(adapter, "free_busy")
        assert hasattr(adapter, "create_event")
        assert hasattr(adapter, "update_event")
        assert hasattr(adapter, "delete_event")
        assert hasattr(adapter, "list_events")


class TestGoogleCalendarAdapter:
    @pytest.mark.asyncio
    async def test_create_event_calls_to_thread(self):
        with patch("app.calendar_client.google_adapter.get_settings") as ms:
            ms.return_value = MagicMock(google_calendar_credentials="", google_calendar_delegation_email=None)
            adapter = GoogleCalendarAdapter()

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = {"id": "evt-new-123", "summary": "Consultation"}
            result = await adapter.create_event(
                calendar_id="cal-1",
                summary="Consultation",
                start_iso="2026-04-10T10:00:00Z",
                end_iso="2026-04-10T11:00:00Z",
            )
            assert result["id"] == "evt-new-123"
            mock_thread.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_event_calls_to_thread(self):
        with patch("app.calendar_client.google_adapter.get_settings") as ms:
            ms.return_value = MagicMock(google_calendar_credentials="", google_calendar_delegation_email=None)
            adapter = GoogleCalendarAdapter()

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = None
            await adapter.delete_event(calendar_id="cal-1", event_id="evt-123")
            mock_thread.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_events_calls_to_thread(self):
        with patch("app.calendar_client.google_adapter.get_settings") as ms:
            ms.return_value = MagicMock(google_calendar_credentials="", google_calendar_delegation_email=None)
            adapter = GoogleCalendarAdapter()

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = [{"id": "evt-1"}, {"id": "evt-2"}]
            result = await adapter.list_events(
                calendar_id="cal-1",
                time_min_iso="2026-04-10T00:00:00Z",
                time_max_iso="2026-04-10T23:59:59Z",
            )
            assert len(result) == 2
