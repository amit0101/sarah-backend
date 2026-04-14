"""Google Calendar API adapter."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build

from app.config import get_settings

logger = logging.getLogger(__name__)


class GoogleCalendarAdapter:
    """Uses a service account JSON file from GOOGLE_CALENDAR_CREDENTIALS."""

    def __init__(self, credentials_path: Optional[str] = None) -> None:
        settings = get_settings()
        path = credentials_path or settings.google_calendar_credentials
        self._path = path
        self._delegation = settings.google_calendar_delegation_email
        self._service: Any = None

    def _ensure(self) -> Any:
        if self._service is not None:
            return self._service
        if not self._path:
            raise RuntimeError("GOOGLE_CALENDAR_CREDENTIALS not set")
        scopes = ["https://www.googleapis.com/auth/calendar"]
        # Support both a file path and a raw JSON string (for cloud deployments
        # where mounting files isn't possible — paste the JSON as the env var value).
        raw = self._path.strip()
        if raw.startswith("{"):
            info = json.loads(raw)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=scopes
            )
        else:
            creds = service_account.Credentials.from_service_account_file(
                raw, scopes=scopes
            )
        if self._delegation:
            creds = creds.with_subject(self._delegation)
        self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return self._service

    async def free_busy(
        self,
        calendar_id: str,
        *,
        time_min_iso: str,
        time_max_iso: str,
        timezone: str,
    ) -> List[Dict[str, Any]]:
        def _call() -> None:
            svc = self._ensure()
            body = {
                "timeMin": time_min_iso,
                "timeMax": time_max_iso,
                "timeZone": timezone,
                "items": [{"id": calendar_id}],
            }
            return svc.freebusy().query(body=body).execute()

        raw = await asyncio.to_thread(_call)
        cal = (raw.get("calendars") or {}).get(calendar_id) or {}
        return list(cal.get("busy") or [])

    async def create_event(
        self,
        calendar_id: str,
        *,
        start_iso: str,
        end_iso: str,
        summary: str,
        description: str | None = None,
    ) -> Dict[str, Any]:
        def _call() -> None:
            svc = self._ensure()
            body: Dict[str, Any] = {
                "summary": summary,
                "start": {"dateTime": start_iso},
                "end": {"dateTime": end_iso},
            }
            if description:
                body["description"] = description
            return (
                svc.events()
                .insert(calendarId=calendar_id, body=body)
                .execute()
            )

        return await asyncio.to_thread(_call)

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
        """PATCH an existing event — only supplied fields are updated."""

        def _call() -> Dict[str, Any]:
            svc = self._ensure()
            body: Dict[str, Any] = {}
            if start_iso:
                body["start"] = {"dateTime": start_iso}
            if end_iso:
                body["end"] = {"dateTime": end_iso}
            if summary is not None:
                body["summary"] = summary
            if description is not None:
                body["description"] = description
            return (
                svc.events()
                .patch(calendarId=calendar_id, eventId=event_id, body=body)
                .execute()
            )

        return await asyncio.to_thread(_call)

    async def delete_event(
        self,
        calendar_id: str,
        event_id: str,
    ) -> None:
        """Delete/cancel a calendar event."""

        def _call() -> None:
            svc = self._ensure()
            svc.events().delete(calendarId=calendar_id, eventId=event_id).execute()

        await asyncio.to_thread(_call)

    async def list_events(
        self,
        calendar_id: str,
        *,
        time_min_iso: str,
        time_max_iso: str,
    ) -> List[Dict[str, Any]]:
        """List events in a time range (up to 50)."""

        def _call() -> List[Dict[str, Any]]:
            svc = self._ensure()
            result = (
                svc.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=time_min_iso,
                    timeMax=time_max_iso,
                    maxResults=50,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            return result.get("items", [])

        return await asyncio.to_thread(_call)

