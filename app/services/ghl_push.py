"""GHL push wrappers for calendar_service.

The booking engine in `app/services/calendar_service.py` accepts injected
callables for GHL writes (create/update/cancel) so it stays unit-testable
and lets multiple call-sites use the same helpers. Two consumers exist:

  * Sarah's tool runner (`app/services/sarah_tools.py`) — invoked from the
    OpenAI Responses turn loop with a fully populated ToolContext.

  * The internal HTTP bridge (`app/api/routes/internal.py`) — invoked by
    the comms-platform backend with raw arguments (organization id,
    location id, ghl contact id) and a freshly constructed GHLClient.

Both paths previously had to re-implement the same three closures. This
module hosts module-level factories that take the minimal raw arguments
they actually need so the two call-sites can share them.

Failures inside any push are caught and logged — never raised — so a GHL
sync hiccup never aborts a Google Calendar booking. The booking row in
`sarah.appointments` and the Google event-of-record remain authoritative;
GHL is a mirror per APPOINTMENTS_ARCHITECTURE.md §6.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from app.ghl_client import GHLClient
from app.ghl_client import calendars as ghl_cal
from app.models.appointment import Appointment

logger = logging.getLogger(__name__)


GhlCreatePush = Callable[[Appointment], Awaitable[Optional[str]]]
GhlUpdatePush = Callable[[Appointment, datetime, datetime], Awaitable[None]]
GhlCancelPush = Callable[[Appointment], Awaitable[None]]


def make_create_push(
    ghl: GHLClient,
    *,
    ghl_calendar_id: Optional[str],
    ghl_location_id: str,
    ghl_contact_id: Optional[str],
) -> GhlCreatePush:
    """Return a coroutine that mirrors a new Sarah appointment to GHL.

    Returns the GHL appointment id on success, or None when the GHL
    surface is misconfigured (missing calendar id or contact id) or
    the API call fails.
    """

    async def _push(appt: Appointment) -> Optional[str]:
        if not ghl_calendar_id or not ghl_contact_id:
            return None
        try:
            resp = await ghl_cal.create_appointment(
                ghl,
                ghl_calendar_id,
                location_id=ghl_location_id,
                contact_id=ghl_contact_id,
                start_time=appt.starts_at.isoformat(),
                end_time=appt.ends_at.isoformat(),
                title=appt.service_type.replace("_", " ").title(),
                notes=appt.notes,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("GHL create_appointment failed: %s", e)
            return None
        ghl_id = resp.get("id") or resp.get("appointment", {}).get("id")
        return str(ghl_id) if ghl_id else None

    return _push


def make_update_push(
    ghl: GHLClient,
    *,
    ghl_location_id: str,
) -> GhlUpdatePush:
    async def _push(appt: Appointment, new_start: Any, new_end: Any) -> None:
        if not appt.ghl_appointment_id:
            return
        try:
            await ghl_cal.update_appointment(
                ghl,
                appt.ghl_appointment_id,
                location_id=ghl_location_id,
                startTime=new_start.isoformat(),
                endTime=new_end.isoformat(),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("GHL update_appointment failed: %s", e)

    return _push


def make_cancel_push(
    ghl: GHLClient,
    *,
    ghl_location_id: str,
) -> GhlCancelPush:
    async def _push(appt: Appointment) -> None:
        if not appt.ghl_appointment_id:
            return
        try:
            await ghl_cal.cancel_appointment(
                ghl,
                appt.ghl_appointment_id,
                location_id=ghl_location_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("GHL cancel_appointment failed: %s", e)

    return _push
