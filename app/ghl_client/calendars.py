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
    ignore_date_range: bool = True,
) -> Dict[str, Any]:
    """POST /calendars/events/appointments — GHL API V2 (calendarId in body).

    The earlier `POST /calendars/{calendarId}/appointments` form returns 404;
    GHL canonical V2 puts the calendar id in the request body and uses a
    fixed endpoint path under `/calendars/events/`. Verified manually against
    the live GHL API on 2026-05-04.

    `ignore_date_range` defaults to True because Sarah owns slot validation
    upstream (Google availability + `_filter_future_slots` lead buffer). The
    GHL calendar's own `allowBookingAfter` / `slotDuration` / `slotInterval`
    rules are tuned for the public booking widget and would otherwise refuse
    pushes for slots Sarah has already promised to the customer (e.g.
    Preplanning Calendar requires 2-day lead time → next-day bookings 400).
    Past-time pushes are still rejected by GHL even with this flag set, which
    is the desired safety net.
    """
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
    if ignore_date_range:
        body["ignoreDateRange"] = True
    return await client.request(
        "POST",
        "/calendars/events/appointments",
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
        f"/calendars/events/appointments/{appointment_id}",
        location_id=location_id,
        json_body=body,
    )


async def cancel_appointment(
    client: GHLClient,
    appointment_id: str,
    *,
    location_id: str,
) -> Dict[str, Any]:
    """DELETE /calendars/events/appointments/{id} — cancel/remove appointment."""
    return await client.request(
        "DELETE",
        f"/calendars/events/appointments/{appointment_id}",
        location_id=location_id,
    )
