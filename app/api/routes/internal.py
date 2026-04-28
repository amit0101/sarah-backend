"""Internal API for Comms Platform — Section 9.4.

Surfaces two families of read-only/operational endpoints that the comms
backend talks to over HTTP:

  1. Conversation / contact lookups (existing) — used by the comms inbox
     to back-resolve Sarah-originated conversations into the comms
     timeline.

  2. Calendar bridge (added 2026-04-20 for W3) — proxies the
     `app/services/calendar_service.py` typed-pool booking engine so the
     comms platform never has to reach across schemas or re-implement
     the algorithm. See `sarah-podium-plan/APPOINTMENTS_ARCHITECTURE.md`
     §3 for the booking model and §4.4 for the endpoint contract.

Auth model
----------
The conversation/contact endpoints are open today (they were added when
both services lived behind the same private network). The calendar
bridge endpoints require `X-Webhook-Secret == settings.sarah_webhook_secret`
because they perform writes (Google Calendar + GHL + sarah.appointments).
The shared secret lives on Sarah Render as SARAH_WEBHOOK_SECRET and on
comms-platform-backend Render as the same value (the comms side calls
its env var SARAH_WEBHOOK_SECRET — same name, same value).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date as _date, datetime
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.calendar_client.google_adapter import GoogleCalendarAdapter
from app.config import get_settings
from app.database.session import DbSession
from app.ghl_client.factory import get_ghl_client_for_org
from app.models.appointment import Appointment
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.location import Location
from app.models.message import Message
from app.models.organization import Organization
from app.services import calendar_service as cal_svc
from app.services import ghl_push

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["internal"])


# ─── Conversation / contact lookups (pre-existing) ───────────────────────────


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    conv = await db.get(Conversation, conversation_id)
    if not conv:
        return {"error": "not_found"}
    return {
        "id": str(conv.id),
        "organization_id": str(conv.organization_id),
        "contact_id": str(conv.contact_id),
        "location_id": conv.location_id,
        "channel": conv.channel,
        "mode": conv.mode,
        "status": conv.status,
        "openai_response_id": conv.openai_response_id,
        "active_path": conv.active_path,
    }


@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    r = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )
    rows = r.scalars().all()
    return {
        "conversation_id": str(conversation_id),
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "channel": m.channel,
                "created_at": m.created_at.isoformat(),
            }
            for m in rows
        ],
    }


@router.get("/contacts/{ghl_contact_id}/conversations")
async def list_contact_conversations(
    ghl_contact_id: str,
    db: DbSession,
    organization_id: Optional[uuid.UUID] = Query(
        None,
        description="Required when the same GHL id could exist in multiple orgs",
    ),
) -> dict[str, Any]:
    q = select(Contact).where(Contact.ghl_contact_id == ghl_contact_id)
    if organization_id:
        q = q.where(Contact.organization_id == organization_id)
    r = await db.execute(q)
    contacts = r.scalars().all()
    if not contacts:
        return {"conversations": []}
    if len(contacts) > 1 and not organization_id:
        return {
            "error": "ambiguous_contact",
            "message": "Pass organization_id to disambiguate",
        }
    c = contacts[0]
    r2 = await db.execute(select(Conversation).where(Conversation.contact_id == c.id))
    convs = r2.scalars().all()
    return {
        "organization_id": str(c.organization_id),
        "conversations": [{"id": str(x.id), "status": x.status} for x in convs],
    }


# ─── Calendar bridge (W3 — APPOINTMENTS_ARCHITECTURE.md §4.4) ────────────────


def _require_webhook_secret(secret: Optional[str]) -> None:
    """Reject calendar-bridge calls that don't carry the shared secret."""
    expected = get_settings().sarah_webhook_secret
    if not expected:
        # Misconfigured Sarah deployment — refuse rather than silently allow.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="sarah_webhook_secret not configured",
        )
    if secret != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid X-Webhook-Secret",
        )


async def _resolve_org(
    db: DbSession,
    organization_id: Optional[uuid.UUID],
    organization_slug: Optional[str],
) -> Organization:
    """Either organization_id or organization_slug must be supplied.

    The comms platform speaks slugs (its `Organization.slug` matches the
    Sarah `organizations.slug` by convention — both currently 'mhc').
    Internal callers may also pass the raw uuid.
    """
    if organization_id is None and not organization_slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="organization_id or organization_slug required",
        )
    if organization_id is not None:
        org = await db.get(Organization, organization_id)
    else:
        r = await db.execute(
            select(Organization).where(Organization.slug == organization_slug)
        )
        org = r.scalar_one_or_none()
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="organization not found",
        )
    return org


def _ghl_scope(org: Organization, location: Optional[Location]) -> str:
    if location is not None and location.ghl_location_id:
        return location.ghl_location_id
    return org.ghl_location_id


@router.get("/internal/org/feature-flags")
async def get_feature_flags(
    db: DbSession,
    organization_id: Optional[uuid.UUID] = Query(None),
    organization_slug: Optional[str] = Query(None),
    x_webhook_secret: Optional[str] = Header(None, alias="X-Webhook-Secret"),
) -> dict[str, Any]:
    """Expose `organizations.config.feature_flags` to comms.

    Used by `useFeatureFlags()` in the comms frontend (via the
    comms-side `/api/org/feature-flags` proxy) to decide whether to
    render the venue picker (`room_calendars_enabled`) and the
    Pre-arranger dropdown (`pre_arrangers_enabled`).
    """
    _require_webhook_secret(x_webhook_secret)
    org = await _resolve_org(db, organization_id, organization_slug)
    flags = cal_svc.FeatureFlags.from_org(org)
    return {
        "organization_id": str(org.id),
        "organization_slug": org.slug,
        "feature_flags": {
            "room_calendars_enabled": flags.room_calendars_enabled,
            "pre_arrangers_enabled": flags.pre_arrangers_enabled,
        },
    }


@router.get("/internal/calendar/availability")
async def get_calendar_availability(
    db: DbSession,
    intent: str = Query(..., pattern="^(at_need|pre_need)$"),
    location_slug: str = Query(...),
    target_date: _date = Query(...),
    duration_minutes: int = Query(60, ge=15, le=480),
    timezone: str = Query("America/Edmonton"),
    venue_calendar_google_id: Optional[str] = Query(None),
    booking_calendar_google_id: Optional[str] = Query(None),
    max_slots: int = Query(3, ge=1, le=20),
    organization_id: Optional[uuid.UUID] = Query(None),
    organization_slug: Optional[str] = Query(None),
    x_webhook_secret: Optional[str] = Header(None, alias="X-Webhook-Secret"),
) -> dict[str, Any]:
    """Run `cal_svc.propose_slots` and serialise the result.

    For at-need M&H, callers should pass `booking_calendar_google_id` (the
    shared director-bookings calendar — typically the location's calendar_id).
    Returns `{slots: []}` when no roster is seeded yet.
    """
    _require_webhook_secret(x_webhook_secret)
    org = await _resolve_org(db, organization_id, organization_slug)

    if not booking_calendar_google_id:
        location = await db.get(Location, (org.id, location_slug))
        booking_calendar_google_id = location.calendar_id if location else None

    proposals = await cal_svc.propose_slots(
        db=db,
        calendar=GoogleCalendarAdapter(),
        organization=org,
        intent=intent,
        location_slug=location_slug,
        target_date=target_date,
        timezone=timezone,
        duration_minutes=duration_minutes,
        venue_calendar_google_id=venue_calendar_google_id,
        booking_calendar_google_id=booking_calendar_google_id,
        max_slots=max_slots,
    )
    return {
        "organization_id": str(org.id),
        "intent": intent,
        "location_slug": location_slug,
        "target_date": target_date.isoformat(),
        "timezone": timezone,
        "slots": [
            {
                "starts_at": s.starts_at.isoformat(),
                "ends_at": s.ends_at.isoformat(),
                "primary_calendar_id": s.primary_calendar_id,
                "primary_label": s.primary_label,
                "venue_calendar_id": s.venue_calendar_id,
                "venue_label": s.venue_label,
                "metadata": s.metadata,
            }
            for s in proposals
        ],
    }


class _SlotIn(BaseModel):
    starts_at: datetime
    ends_at: datetime
    primary_calendar_id: str
    primary_label: str
    venue_calendar_id: Optional[str] = None
    venue_label: Optional[str] = None


class CalendarBookRequest(BaseModel):
    organization_id: Optional[uuid.UUID] = None
    organization_slug: Optional[str] = None
    contact_id: uuid.UUID
    location_slug: str
    intent: str = Field(..., pattern="^(at_need|pre_need)$")
    service_type: str
    slot: _SlotIn
    created_by: str = Field("staff", pattern="^(sarah|staff|ghl|self_service)$")
    conversation_id: Optional[uuid.UUID] = None
    notes: Optional[str] = None
    summary: Optional[str] = None
    description: Optional[str] = None


class CalendarRescheduleRequest(BaseModel):
    new_starts_at: datetime
    new_ends_at: datetime
    notes: Optional[str] = None


@router.post("/internal/calendar/appointments")
async def create_calendar_appointment(
    payload: CalendarBookRequest,
    db: DbSession,
    x_webhook_secret: Optional[str] = Header(None, alias="X-Webhook-Secret"),
) -> dict[str, Any]:
    """Materialise a previously-proposed slot.

    Mirrors `sarah_tools._book_appointment` for staff-initiated bookings
    from the comms inbox: writes Google Calendar event-of-record (+
    optional venue hold), GHL appointment, and the canonical
    sarah.appointments row.
    """
    _require_webhook_secret(x_webhook_secret)

    org = await _resolve_org(db, payload.organization_id, payload.organization_slug)
    contact = await db.get(Contact, payload.contact_id)
    if contact is None or contact.organization_id != org.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="contact not found for this organization",
        )

    location = await db.get(Location, (org.id, payload.location_slug))
    ghl_calendar_id = location.ghl_calendar_id if location else None
    ghl_location_id = _ghl_scope(org, location)
    ghl = get_ghl_client_for_org(org)
    push = ghl_push.make_create_push(
        ghl,
        ghl_calendar_id=ghl_calendar_id,
        ghl_location_id=ghl_location_id,
        ghl_contact_id=contact.ghl_contact_id,
    )

    slot = cal_svc.SlotProposal(
        starts_at=payload.slot.starts_at,
        ends_at=payload.slot.ends_at,
        primary_calendar_id=payload.slot.primary_calendar_id,
        primary_label=payload.slot.primary_label,
        venue_calendar_id=payload.slot.venue_calendar_id,
        venue_label=payload.slot.venue_label,
    )

    try:
        appt = await cal_svc.confirm_booking(
            db=db,
            calendar=GoogleCalendarAdapter(),
            organization=org,
            slot=slot,
            contact=contact,
            intent=payload.intent,
            service_type=payload.service_type,
            created_by=payload.created_by,
            conversation_id=payload.conversation_id,
            notes=payload.notes,
            summary=payload.summary,
            description=payload.description,
            push_to_ghl=push,
        )
    except Exception:
        logger.exception(
            "internal_confirm_booking_failed org=%s contact=%s",
            org.id,
            contact.id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="booking failed",
        )

    return _appointment_to_dict(appt)


@router.put("/internal/calendar/appointments/{appointment_id}")
async def reschedule_calendar_appointment(
    appointment_id: uuid.UUID,
    payload: CalendarRescheduleRequest,
    db: DbSession,
    organization_id: Optional[uuid.UUID] = Query(None),
    organization_slug: Optional[str] = Query(None),
    x_webhook_secret: Optional[str] = Header(None, alias="X-Webhook-Secret"),
) -> dict[str, Any]:
    _require_webhook_secret(x_webhook_secret)
    org = await _resolve_org(db, organization_id, organization_slug)

    appt = await db.get(Appointment, appointment_id)
    if appt is None or appt.organization_id != org.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="appointment not found for this organization",
        )

    # sarah.appointments does not currently store location_id — fall back
    # to the org-level GHL location scope (correct for MHFH single sub-account).
    ghl = get_ghl_client_for_org(org)
    push = ghl_push.make_update_push(ghl, ghl_location_id=_ghl_scope(org, None))

    try:
        updated = await cal_svc.reschedule_booking(
            db=db,
            calendar=GoogleCalendarAdapter(),
            organization=org,
            appointment=appt,
            new_starts_at=payload.new_starts_at,
            new_ends_at=payload.new_ends_at,
            notes=payload.notes,
            push_to_ghl=push,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        logger.exception(
            "internal_reschedule_failed appointment_id=%s", appt.id
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="reschedule failed",
        )

    return _appointment_to_dict(updated)


@router.delete("/internal/calendar/appointments/{appointment_id}")
async def cancel_calendar_appointment(
    appointment_id: uuid.UUID,
    db: DbSession,
    organization_id: Optional[uuid.UUID] = Query(None),
    organization_slug: Optional[str] = Query(None),
    x_webhook_secret: Optional[str] = Header(None, alias="X-Webhook-Secret"),
) -> dict[str, Any]:
    _require_webhook_secret(x_webhook_secret)
    org = await _resolve_org(db, organization_id, organization_slug)

    appt = await db.get(Appointment, appointment_id)
    if appt is None or appt.organization_id != org.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="appointment not found for this organization",
        )

    ghl = get_ghl_client_for_org(org)
    push = ghl_push.make_cancel_push(ghl, ghl_location_id=_ghl_scope(org, None))

    try:
        cancelled = await cal_svc.cancel_booking(
            db=db,
            calendar=GoogleCalendarAdapter(),
            organization=org,
            appointment=appt,
            push_to_ghl=push,
        )
    except Exception:
        logger.exception(
            "internal_cancel_failed appointment_id=%s", appt.id
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="cancel failed",
        )

    return _appointment_to_dict(cancelled)


@router.get("/internal/calendar/appointments")
async def list_calendar_appointments(
    db: DbSession,
    contact_id: Optional[uuid.UUID] = Query(None),
    organization_id: Optional[uuid.UUID] = Query(None),
    organization_slug: Optional[str] = Query(None),
    starts_after: Optional[datetime] = Query(None),
    starts_before: Optional[datetime] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    x_webhook_secret: Optional[str] = Header(None, alias="X-Webhook-Secret"),
) -> dict[str, Any]:
    """Appointments timeline for the contact-profile pane.

    Returns scheduled + rescheduled + cancelled rows so the UI can
    render a complete history. Callers filter by status client-side.
    """
    _require_webhook_secret(x_webhook_secret)
    org = await _resolve_org(db, organization_id, organization_slug)

    stmt = select(Appointment).where(Appointment.organization_id == org.id)
    if contact_id is not None:
        stmt = stmt.where(Appointment.contact_id == contact_id)
    if starts_after is not None:
        stmt = stmt.where(Appointment.starts_at >= starts_after)
    if starts_before is not None:
        stmt = stmt.where(Appointment.starts_at < starts_before)
    stmt = stmt.order_by(Appointment.starts_at.desc()).limit(limit)

    rows = (await db.execute(stmt)).scalars().all()
    return {
        "organization_id": str(org.id),
        "appointments": [_appointment_to_dict(a) for a in rows],
    }


def _appointment_to_dict(a: Appointment) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "organization_id": str(a.organization_id),
        "contact_id": str(a.contact_id) if a.contact_id else None,
        "conversation_id": str(a.conversation_id) if a.conversation_id else None,
        "service_type": a.service_type,
        "intent": a.intent,
        "starts_at": a.starts_at.isoformat(),
        "ends_at": a.ends_at.isoformat(),
        "primary_cal_id": str(a.primary_cal_id) if a.primary_cal_id else None,
        "venue_cal_id": str(a.venue_cal_id) if a.venue_cal_id else None,
        "google_event_id": a.google_event_id,
        "google_venue_event_id": a.google_venue_event_id,
        "ghl_appointment_id": a.ghl_appointment_id,
        "status": a.status,
        "created_by": a.created_by,
        "notes": a.notes,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }
