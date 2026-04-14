"""CalendarClient protocol."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class CalendarClient(Protocol):
    async def free_busy(
        self,
        calendar_id: str,
        *,
        time_min_iso: str,
        time_max_iso: str,
        timezone: str,
    ) -> List[Dict[str, Any]]:
        """Return busy blocks or free slots metadata."""

    async def create_event(
        self,
        calendar_id: str,
        *,
        start_iso: str,
        end_iso: str,
        summary: str,
        description: str | None = None,
    ) -> Dict[str, Any]:
        """Create a calendar event."""

    async def update_event(
        self,
        calendar_id: str,
        event_id: str,
        *,
        start_iso: Optional[str] = None,
        end_iso: Optional[str] = None,
        summary: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update/reschedule an existing calendar event."""

    async def delete_event(
        self,
        calendar_id: str,
        event_id: str,
    ) -> None:
        """Delete/cancel a calendar event."""

    async def list_events(
        self,
        calendar_id: str,
        *,
        time_min_iso: str,
        time_max_iso: str,
    ) -> List[Dict[str, Any]]:
        """List events in a time range."""

