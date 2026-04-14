"""Calendar abstraction — Section 4.5."""

from app.calendar_client.base import CalendarClient
from app.calendar_client.google_adapter import GoogleCalendarAdapter

__all__ = ["CalendarClient", "GoogleCalendarAdapter"]
