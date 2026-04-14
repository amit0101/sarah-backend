"""GHL calendar availability and appointments — API V2."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.ghl_client.client import GHLClient


async def get_free_slots(
    client: GHLClient,
    calendar_id: str,
    *,
    location_id: str,
    start_ms: int,
    end_ms: int,
    timezone: str = "America/Edmonton",
) -> List[Dict[str, Any]]:
    """GET /calendars/{calendarId}/free-slots — time range in epoch ms."""
    params = {
        "startDate": start_ms,
        "endDate": end_ms,
        "timezone": timezone,
    }
    data = await client.request(
        "GET",
        f"/calendars/{calendar_id}/free-slots",
        location_id=location_id,
        params=params,
    )
    if isinstance(data, dict):
        slots = data.get("slots") or data.get("traceId") and data.get("slots")
        if isinstance(data.get("slots"), list):
            return list(data["slots"])
        if isinstance(data.get("freeSlots"), list):
            return list(data["freeSlots"])
    if isinstance(data, list):
        return data
    return []


async def create_appointment(
    client: GHLClient,
    calendar_id: str,
    *,
    location_id: str,
    contact_id: str,
    start_time: str,
    end_time: Optional[str] = None,
    title: Optional[str] = None,
    appointment_status: str = "confirmed",
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """POST /calendars/{calendarId}/appointments — ISO times per GHL."""
    body: Dict[str, Any] = {
        "calendarId": calendar_id,
        "locationId": location_id,
        "contactId": contact_id,
        "startTime": start_time,
        "appointmentStatus": appointment_status,
    }
    if end_time:
        body["endTime"] = end_time
    if title:
        body["title"] = title
    if notes:
        body["notes"] = notes
    return await client.request(
        "POST",
        f"/calendars/{calendar_id}/appointments",
        location_id=location_id,
        json_body=body,
    )


async def update_appointment(
    client: GHLClient,
    appointment_id: str,
    *,
    location_id: str,
    **fields: Any,
) -> Dict[str, Any]:
    body = {k: v for k, v in fields.items() if v is not None}
    return await client.request(
        "PUT",
        f"/calendars/appointments/{appointment_id}",
        location_id=location_id,
        json_body=body,
    )


async def cancel_appointment(
    client: GHLClient,
    appointment_id: str,
    *,
    location_id: str,
) -> Dict[str, Any]:
    """DELETE /calendars/appointments/{id} — cancel/remove appointment."""
    return await client.request(
        "DELETE",
        f"/calendars/appointments/{appointment_id}",
        location_id=location_id,
    )
