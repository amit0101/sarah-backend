"""Inbound GHL → Sarah appointment sync.

Used by `POST /webhooks/ghl/{org_slug}/appointment` to upsert a row into
`sarah.appointments` whenever a GHL appointment is created, updated, or
cancelled outside the Sarah/comms-platform write paths.

Why this layer exists
---------------------
Today the Sarah/comms write paths (`book_appointment` tool, comms-platform
calendar UI proxy) write `sarah.appointments` directly AND push to GHL.
But staff can also book/edit/cancel directly inside the GHL UI (or via a
synced Google calendar that GHL is configured to round-trip). Without this
webhook, those mutations never reach `sarah.appointments` and Sarah's
availability calculation drifts out of sync with the actual operating
state. See SESSION_HANDOFF.md "Carried into session 15 — Priority B".

Idempotency + double-write avoidance
------------------------------------
GHL fires a workflow on Sarah-originated bookings too (because Sarah
pushes to GHL via `_make_ghl_create_push`). If we upsert on every event
we'll occasionally overwrite a Sarah-written row with a slightly later
GHL-shaped representation (status='confirmed' vs 'scheduled' etc). Two
defenses:

  1. **`source_channel` short-circuit.** When the event's
     `source_channel == 'sarah'` AND a row with that `ghl_appointment_id`
     already exists, we no-op. (Sarah's row is already authoritative.)
     The operator's GHL workflow is responsible for stamping
     `source_channel = 'sarah'` when the event was created via Sarah's
     push — easiest done by mapping it from the contact custom field of
     the same name (which Sarah sets to 'webchat'/'sms' on contact
     creation).

  2. **Status-mapping doesn't downgrade.** If the row exists with
     `status='cancelled'` and the inbound event says `'scheduled'` we
     still apply it (a re-book is a real state change). The only thing
     we *don't* do is overwrite a Sarah-written `primary_cal_id` /
     `venue_cal_id` with NULL just because GHL's payload didn't carry
     them — those fields are kept on existing rows.

Calendar resolution
-------------------
Inbound events typically don't include sarah.calendars UUIDs (GHL knows
its own calendar id, not ours). We don't try to reverse-resolve
`primary_cal_id` from the GHL calendar id — staff bookings made outside
Sarah may not even land on a calendar Sarah tracks (GHL has its own
per-location calendars too). We persist the GHL-side ids
(`google_event_id` if present, `ghl_appointment_id` always) and leave
the FK columns NULL when we can't resolve. Sarah's availability calc
uses the Google free/busy reads on her calendars, not these FK rows, so
this is safe.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment
from app.models.contact import Contact
from app.models.organization import Organization

logger = logging.getLogger(__name__)


# ─── Canonical inbound payload ───────────────────────────────────────────────


# What we accept on the wire. GHL workflows are configured to POST this
# shape via a "Custom Webhook" action — operator builds the body once
# from GHL merge fields. See the operator runbook in
# `sarah-podium-plan/GHL_APPOINTMENT_WEBHOOK_SETUP.md`.
GhlInboundStatus = Literal[
    "new",
    "confirmed",
    "scheduled",
    "rescheduled",
    "cancelled",
    "no_show",
    "completed",
    "showed",
    "noshow",
]


class GhlAppointmentEvent(BaseModel):
    """Canonical GHL → Sarah appointment payload.

    Fields are intentionally permissive (most are optional) because
    GHL workflows often can't supply every datum on every event type
    (e.g. a `cancelled` event may not include `starts_at`). The handler
    only requires `ghl_appointment_id`. Everything else is best-effort.
    """

    ghl_appointment_id: str = Field(..., min_length=1)
    ghl_contact_id: Optional[str] = None
    status: GhlInboundStatus
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    title: Optional[str] = None
    notes: Optional[str] = None
    google_event_id: Optional[str] = None
    # GHL's own calendar id (a string GUID GHL chose). Stored under the
    # appointment metadata in `notes` for forensic value but not
    # FK-resolved against `sarah.calendars`.
    ghl_calendar_id: Optional[str] = None
    # Custom-field-style hint set on the contact when Sarah originated
    # the booking. When present and equal to one of the Sarah origins,
    # the upsert is a no-op (idempotency rule 1 above).
    source_channel: Optional[str] = None

    @field_validator("ghl_appointment_id")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ghl_appointment_id required")
        return v


# Statuses we treat as "Sarah's row should not be overwritten". The
# operator will typically map the contact's `source_channel` custom
# field directly into the webhook body; Sarah sets that field to one
# of {webchat, sms, sarah_handover}. Staff-side manual bookings will
# either leave it blank or stamp it with 'ghl_ui'/'comms_ui'/etc.
_SARAH_ORIGIN_CHANNELS = {"webchat", "sms", "sarah", "sarah_handover"}


# Map the inbound GHL-shaped status to our enum. APPOINTMENT_STATUSES is
# {scheduled, rescheduled, cancelled, no_show, completed}. Mapping is
# direct — `is_update` is accepted for symmetry with the upsert call site
# but no longer alters the result (we previously flipped 'new'/'confirmed'
# to 'rescheduled' on updates which gave false positives whenever GHL
# re-fired the same workflow). The operator's workflow distinguishes
# reschedules explicitly via the `rescheduled` inbound status.
def _map_status(inbound: str, *, is_update: bool = False) -> str:  # noqa: ARG001
    s = (inbound or "").strip().lower()
    if s in {"cancelled", "canceled"}:
        return "cancelled"
    if s in {"no_show", "noshow"}:
        return "no_show"
    if s in {"completed", "showed"}:
        return "completed"
    if s == "rescheduled":
        return "rescheduled"
    # 'new', 'confirmed', 'scheduled' — booking is on the books.
    return "scheduled"


def _is_sarah_origin(source_channel: Optional[str]) -> bool:
    if not source_channel:
        return False
    return source_channel.strip().lower() in _SARAH_ORIGIN_CHANNELS


async def _resolve_contact(
    db: AsyncSession, org_id: uuid.UUID, ghl_contact_id: Optional[str]
) -> Optional[Contact]:
    if not ghl_contact_id:
        return None
    res = await db.execute(
        select(Contact).where(
            Contact.organization_id == org_id,
            Contact.ghl_contact_id == ghl_contact_id,
        )
    )
    return res.scalar_one_or_none()


# ─── Public entrypoint ────────────────────────────────────────────────────────


class SyncOutcome(BaseModel):
    """What the route returns to GHL so the operator can debug from the
    workflow execution log."""

    action: Literal["created", "updated", "ignored_sarah_origin", "ignored_no_change"]
    appointment_id: Optional[str] = None
    ghl_appointment_id: str
    status: Optional[str] = None
    matched_existing: bool


async def upsert_from_ghl(
    db: AsyncSession,
    org: Organization,
    event: GhlAppointmentEvent,
) -> SyncOutcome:
    """Apply a GHL appointment event to `sarah.appointments`.

    Idempotency rules (see module docstring):
      - If a row with this `ghl_appointment_id` exists AND
        `event.source_channel` indicates Sarah-origin, no-op.
      - Otherwise create-or-update keyed on (org_id, ghl_appointment_id).
    """

    res = await db.execute(
        select(Appointment).where(
            Appointment.organization_id == org.id,
            Appointment.ghl_appointment_id == event.ghl_appointment_id,
        )
    )
    existing = res.scalar_one_or_none()

    # Rule 1 — Sarah's row is authoritative. Only ignore when there's
    # already a row to protect; if no row exists we still want to insert
    # (it's possible a Sarah-side write succeeded against GHL but failed
    # against `sarah.appointments`, so the GHL webhook is repairing it).
    if existing is not None and _is_sarah_origin(event.source_channel):
        logger.info(
            "ghl_appointment_webhook ignored sarah-origin event "
            "ghl_id=%s source=%s",
            event.ghl_appointment_id,
            event.source_channel,
        )
        return SyncOutcome(
            action="ignored_sarah_origin",
            appointment_id=str(existing.id),
            ghl_appointment_id=event.ghl_appointment_id,
            status=existing.status,
            matched_existing=True,
        )

    contact = await _resolve_contact(db, org.id, event.ghl_contact_id)
    contact_id = contact.id if contact else None

    new_status = _map_status(event.status, is_update=existing is not None)

    if existing is None:
        # INSERT path. We need at least starts_at + ends_at for the
        # CHECK constraint (`ends_at > starts_at`). If GHL didn't
        # supply both, defer the insert and log — there's no way to
        # synthesize them from a cancellation event with no times.
        if event.starts_at is None or event.ends_at is None:
            logger.warning(
                "ghl_appointment_webhook insert missing times "
                "ghl_id=%s status=%s — ignoring",
                event.ghl_appointment_id,
                event.status,
            )
            return SyncOutcome(
                action="ignored_no_change",
                appointment_id=None,
                ghl_appointment_id=event.ghl_appointment_id,
                status=None,
                matched_existing=False,
            )
        appt = Appointment(
            organization_id=org.id,
            contact_id=contact_id,
            service_type=_infer_service_type(event.title),
            intent=_infer_intent(event.title),
            starts_at=event.starts_at,
            ends_at=event.ends_at,
            primary_cal_id=None,
            venue_cal_id=None,
            google_event_id=event.google_event_id,
            ghl_appointment_id=event.ghl_appointment_id,
            status=new_status,
            created_by="ghl",
            notes=event.notes,
        )
        db.add(appt)
        await db.flush()
        logger.info(
            "ghl_appointment_webhook created ghl_id=%s appt_id=%s status=%s",
            event.ghl_appointment_id,
            appt.id,
            new_status,
        )
        return SyncOutcome(
            action="created",
            appointment_id=str(appt.id),
            ghl_appointment_id=event.ghl_appointment_id,
            status=new_status,
            matched_existing=False,
        )

    # UPDATE path. Apply non-null fields; never overwrite primary/venue
    # FK columns (those are Sarah-side only — see calendar resolution
    # note in module docstring).
    changed = False

    if event.starts_at is not None and event.starts_at != existing.starts_at:
        existing.starts_at = event.starts_at
        changed = True
    if event.ends_at is not None and event.ends_at != existing.ends_at:
        existing.ends_at = event.ends_at
        changed = True
    if new_status != existing.status:
        existing.status = new_status
        changed = True
    if event.notes is not None and event.notes != existing.notes:
        existing.notes = event.notes
        changed = True
    if (
        event.google_event_id is not None
        and event.google_event_id != existing.google_event_id
    ):
        existing.google_event_id = event.google_event_id
        changed = True
    if (
        contact_id is not None
        and existing.contact_id != contact_id
    ):
        existing.contact_id = contact_id
        changed = True

    if not changed:
        return SyncOutcome(
            action="ignored_no_change",
            appointment_id=str(existing.id),
            ghl_appointment_id=event.ghl_appointment_id,
            status=existing.status,
            matched_existing=True,
        )

    logger.info(
        "ghl_appointment_webhook updated ghl_id=%s appt_id=%s status=%s",
        event.ghl_appointment_id,
        existing.id,
        existing.status,
    )
    return SyncOutcome(
        action="updated",
        appointment_id=str(existing.id),
        ghl_appointment_id=event.ghl_appointment_id,
        status=existing.status,
        matched_existing=True,
    )


# ─── Heuristics ───────────────────────────────────────────────────────────────


def _infer_service_type(title: Optional[str]) -> str:
    """Best-effort service_type from event title.

    Sarah's outgoing events use the `Arrangement \u2014 …` prefix (see
    `_make_ghl_create_push`). Staff-side titles are free-form. We default
    to 'arrangement_conf' which is the most common at-need shape and a
    safe placeholder for pre-need too. Operators can re-classify later
    if/when needed.
    """
    if title:
        t = title.lower()
        if "consult" in t or "preplan" in t or "pre-plan" in t:
            return "pre_need_consult"
        if "visitation" in t:
            return "visitation"
        if "service" in t and "arrangement" not in t:
            return "service"
        if "transport" in t:
            return "transport"
        if "reception" in t:
            return "reception"
    return "arrangement_conf"


def _infer_intent(title: Optional[str]) -> str:
    if title:
        t = title.lower()
        if "preplan" in t or "pre-plan" in t or "pre-need" in t or "consult" in t:
            return "pre_need"
    return "at_need"


__all__ = [
    "GhlAppointmentEvent",
    "GhlInboundStatus",
    "SyncOutcome",
    "upsert_from_ghl",
    "_is_sarah_origin",
    "_map_status",
    "_infer_service_type",
    "_infer_intent",
]
