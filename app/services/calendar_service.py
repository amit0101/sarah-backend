"""calendar_service — at-need + pre-need booking against the typed calendar inventory.

This is the skeleton implementation of the algorithms documented in
`sarah-podium-plan/APPOINTMENTS_ARCHITECTURE.md`:

  §3.0  Two calendar conventions (events-as-availability vs events-as-busy)
  §3.1  At-need booking flow                  → propose_at_need + confirm
  §3.2  Pre-need booking flow                 → propose_pre_need + confirm
  §4.1  sarah.calendars schema
  §4.2  sarah.appointments schema
  §4.3  organizations.config feature flags

It deliberately keeps the public surface tiny:

    propose_slots(...) -> list[SlotProposal]
    confirm_booking(slot, contact, ...) -> Appointment

so the comms-platform availability endpoint and Sarah's `book_appointment`
tool can both call into the same logic. The Google Calendar adapter is the
only outward-facing dependency; GHL push is wired through a callback so this
service stays unit-testable.

Status: SKELETON. The body of the algorithms is written end-to-end but the
GHL push and webhook dispatch are wired as injected callables that callers
must supply (Sarah's tool runner injects the existing GHL helpers; a future
comms-platform service injects its own). Mark each `# TODO(W3)` site before
flipping the room-calendar feature flag in production.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar_client.base import CalendarClient
from app.models.appointment import Appointment
from app.models.calendar import Calendar
from app.models.contact import Contact
from app.models.organization import Organization
from app.services.scheduling import (
    PRIORITY_DIRECTORS,
    apply_priority_order,
    available_slots,
    filter_counselors_for_region,
    is_strict_territory_slot,
    location_region,
    parse_counselor_from_event,
)

logger = logging.getLogger(__name__)


# ─── Types ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SlotProposal:
    """One mutually-free candidate slot returned to the chat / staff UI."""

    starts_at: datetime
    ends_at: datetime
    primary_calendar_id: str          # Google calendar id of the chosen Primary/Pre-arranger
    primary_label: str                # Human-readable name for confirmation copy
    venue_calendar_id: Optional[str] = None     # Set only when room calendars enabled (at-need)
    venue_label: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FeatureFlags:
    room_calendars_enabled: bool = False
    pre_arrangers_enabled: bool = False

    @classmethod
    def from_org(cls, org: Organization) -> "FeatureFlags":
        cfg = (org.config or {}).get("feature_flags") or {}
        return cls(
            room_calendars_enabled=bool(cfg.get("room_calendars_enabled", False)),
            pre_arrangers_enabled=bool(cfg.get("pre_arrangers_enabled", False)),
        )


# Optional outbound writer; both Sarah and comms-platform inject their own.
GhlPushCallable = Callable[[Appointment], Awaitable[Optional[str]]]
"""Persist appointment to GHL and return the ghl_appointment_id, or None to skip."""


# ─── Public API ───────────────────────────────────────────────────────────────


async def propose_slots(
    *,
    db: AsyncSession,
    calendar: CalendarClient,
    organization: Organization,
    intent: str,                          # 'at_need' | 'pre_need'
    location_slug: str,                   # e.g. 'park_memorial' (for region routing)
    target_date: date,
    timezone: str = "America/Edmonton",
    duration_minutes: int = 60,
    venue_calendar_google_id: Optional[str] = None,   # at-need: which venue does the family want
    max_slots: int = 3,
) -> List[SlotProposal]:
    """Top-level entry: return up to `max_slots` mutually-free proposals.

    Routes to at-need or pre-need flow based on `intent` and the org's
    `pre_arrangers_enabled` flag. See APPOINTMENTS_ARCHITECTURE.md §3.1/§3.2.
    """

    flags = FeatureFlags.from_org(organization)
    tz = _safe_tz(timezone)
    window_start, window_end = _day_window(target_date, tz)

    if intent == "pre_need" and flags.pre_arrangers_enabled:
        return await _propose_pre_need(
            db=db,
            calendar=calendar,
            organization=organization,
            window_start=window_start,
            window_end=window_end,
            duration=timedelta(minutes=duration_minutes),
            tz=tz,
            max_slots=max_slots,
        )

    # Either an at-need request, OR pre-need with pre-arrangers disabled →
    # fall through to the at-need flow which uses Primaries.
    return await _propose_at_need(
        db=db,
        calendar=calendar,
        organization=organization,
        location_slug=location_slug,
        window_start=window_start,
        window_end=window_end,
        duration=timedelta(minutes=duration_minutes),
        tz=tz,
        max_slots=max_slots,
        venue_calendar_google_id=venue_calendar_google_id,
        flags=flags,
    )


async def confirm_booking(
    *,
    db: AsyncSession,
    calendar: CalendarClient,
    organization: Organization,
    slot: SlotProposal,
    contact: Contact,
    intent: str,
    service_type: str,
    created_by: str,                    # 'sarah' | 'staff' | 'ghl' | 'self_service'
    conversation_id: Optional[Any] = None,
    notes: Optional[str] = None,
    summary: Optional[str] = None,
    description: Optional[str] = None,
    push_to_ghl: Optional[GhlPushCallable] = None,
) -> Appointment:
    """Materialise a SlotProposal: write Google events, GHL, sarah.appointments.

    Order of writes matches APPOINTMENTS_ARCHITECTURE.md §3.1 step 6:

        1. Primary/Pre-arranger calendar  (event of record)
        2. Venue calendar                 (only when room_calendars_enabled and slot.venue_calendar_id is set)
        3. GHL appointment                (back-channel, via injected callable)
        4. sarah.appointments row         (single source of truth)

    Failures after step 1 are logged but do not roll back the Google event;
    a reconciliation worker is expected to clean up orphans (W3 follow-up).
    """

    family_label = _family_label(contact)
    event_summary = summary or _default_summary(service_type, family_label, slot)
    event_description = description or _default_description(
        service_type=service_type,
        family_label=family_label,
        slot=slot,
        notes=notes,
        contact=contact,
    )

    primary_event = await calendar.create_event(
        slot.primary_calendar_id,
        start_iso=slot.starts_at.isoformat(),
        end_iso=slot.ends_at.isoformat(),
        summary=event_summary,
        description=event_description,
    )

    venue_event_id: Optional[str] = None
    if slot.venue_calendar_id:
        try:
            venue_event = await calendar.create_event(
                slot.venue_calendar_id,
                start_iso=slot.starts_at.isoformat(),
                end_iso=slot.ends_at.isoformat(),
                summary=event_summary,
                description=event_description,
            )
            venue_event_id = str(venue_event.get("id") or venue_event.get("event_id") or "")
        except Exception:  # noqa: BLE001 — never block primary booking on venue failure
            logger.exception(
                "venue_event_create_failed venue_cal=%s primary_event=%s",
                slot.venue_calendar_id,
                primary_event.get("id"),
            )

    primary_cal_row = await _calendar_by_google_id(db, organization.id, slot.primary_calendar_id)
    venue_cal_row = (
        await _calendar_by_google_id(db, organization.id, slot.venue_calendar_id)
        if slot.venue_calendar_id
        else None
    )

    appt = Appointment(
        organization_id=organization.id,
        contact_id=contact.id,
        conversation_id=conversation_id,
        service_type=service_type,
        intent=intent,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        primary_cal_id=(primary_cal_row.id if primary_cal_row else None),
        venue_cal_id=(venue_cal_row.id if venue_cal_row else None),
        google_event_id=str(primary_event.get("id") or ""),
        google_venue_event_id=venue_event_id,
        status="scheduled",
        created_by=created_by,
        notes=notes,
    )
    db.add(appt)
    await db.flush()  # populate appt.id before GHL push so we can include it

    if push_to_ghl is not None:
        try:
            ghl_id = await push_to_ghl(appt)
            if ghl_id:
                appt.ghl_appointment_id = ghl_id
        except Exception:  # noqa: BLE001
            logger.exception("ghl_push_failed appointment_id=%s", appt.id)

    await db.commit()
    await db.refresh(appt)
    return appt


# ─── Reschedule / cancel (APPOINTMENTS_ARCHITECTURE.md §3.3) ──────────────────


GhlUpdateCallable = Callable[[Appointment, datetime, datetime], Awaitable[None]]
"""Patch GHL appointment to new start/end. Pass None to skip."""

GhlCancelCallable = Callable[[Appointment], Awaitable[None]]
"""Cancel GHL appointment. Pass None to skip."""


async def reschedule_booking(
    *,
    db: AsyncSession,
    calendar: CalendarClient,
    organization: Organization,
    appointment: Appointment,
    new_starts_at: datetime,
    new_ends_at: datetime,
    notes: Optional[str] = None,
    push_to_ghl: Optional[GhlUpdateCallable] = None,
) -> Appointment:
    """Move an existing appointment to a new window.

    Updates Primary/Pre-arranger event-of-record + (when set) the venue hold
    + GHL mirror + the `sarah.appointments` row. Same write order as
    confirm_booking — failures after the primary event are logged, never
    rolled back; reconciliation is a separate concern.
    """
    if new_ends_at <= new_starts_at:
        raise ValueError("new_ends_at must be after new_starts_at")

    primary_cal = await _calendar_by_pk(db, appointment.primary_cal_id)
    if primary_cal is None or not appointment.google_event_id:
        raise ValueError(
            "appointment is missing primary calendar wiring; cannot reschedule"
        )

    await calendar.update_event(
        primary_cal.google_id,
        appointment.google_event_id,
        start_iso=new_starts_at.isoformat(),
        end_iso=new_ends_at.isoformat(),
    )

    if appointment.venue_cal_id and appointment.google_venue_event_id:
        venue_cal = await _calendar_by_pk(db, appointment.venue_cal_id)
        if venue_cal is not None:
            try:
                await calendar.update_event(
                    venue_cal.google_id,
                    appointment.google_venue_event_id,
                    start_iso=new_starts_at.isoformat(),
                    end_iso=new_ends_at.isoformat(),
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "venue_event_update_failed appointment_id=%s venue_cal=%s",
                    appointment.id,
                    venue_cal.google_id,
                )

    appointment.starts_at = new_starts_at
    appointment.ends_at = new_ends_at
    appointment.status = "rescheduled"
    if notes is not None:
        appointment.notes = notes

    if push_to_ghl is not None and appointment.ghl_appointment_id:
        try:
            await push_to_ghl(appointment, new_starts_at, new_ends_at)
        except Exception:  # noqa: BLE001
            logger.exception(
                "ghl_reschedule_failed appointment_id=%s", appointment.id
            )

    await db.commit()
    await db.refresh(appointment)
    return appointment


async def cancel_booking(
    *,
    db: AsyncSession,
    calendar: CalendarClient,
    organization: Organization,
    appointment: Appointment,
    push_to_ghl: Optional[GhlCancelCallable] = None,
) -> Appointment:
    """Cancel an appointment everywhere: Primary cal → venue cal → GHL → DB.

    Idempotent on the DB side: cancelling an already-cancelled appointment is
    a no-op.
    """
    if appointment.status == "cancelled":
        return appointment

    primary_cal = await _calendar_by_pk(db, appointment.primary_cal_id)
    if primary_cal is not None and appointment.google_event_id:
        try:
            await calendar.delete_event(
                primary_cal.google_id, appointment.google_event_id
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "primary_event_delete_failed appointment_id=%s primary_cal=%s",
                appointment.id,
                primary_cal.google_id,
            )

    if appointment.venue_cal_id and appointment.google_venue_event_id:
        venue_cal = await _calendar_by_pk(db, appointment.venue_cal_id)
        if venue_cal is not None:
            try:
                await calendar.delete_event(
                    venue_cal.google_id, appointment.google_venue_event_id
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "venue_event_delete_failed appointment_id=%s venue_cal=%s",
                    appointment.id,
                    venue_cal.google_id,
                )

    if push_to_ghl is not None and appointment.ghl_appointment_id:
        try:
            await push_to_ghl(appointment)
        except Exception:  # noqa: BLE001
            logger.exception("ghl_cancel_failed appointment_id=%s", appointment.id)

    appointment.status = "cancelled"
    await db.commit()
    await db.refresh(appointment)
    return appointment


# ─── At-need flow (APPOINTMENTS_ARCHITECTURE.md §3.1) ─────────────────────────


async def _propose_at_need(
    *,
    db: AsyncSession,
    calendar: CalendarClient,
    organization: Organization,
    location_slug: str,
    window_start: datetime,
    window_end: datetime,
    duration: timedelta,
    tz: ZoneInfo,
    max_slots: int,
    venue_calendar_google_id: Optional[str],
    flags: FeatureFlags,
) -> List[SlotProposal]:
    """At-need slot proposals against fixed 09:00 / 12:15 / 15:00 grid.

    Routing rules from `mh_venues_brief.html` (2026-04-25):
      Rule 1 (territory).        9am bookings must use a director whose tag
                                 matches the location's territory (N or S).
      Rule 2 (continuity).       12:15 / 15:00 prefer the director who was at
                                 this same site in their previous slot;
                                 otherwise any available director works.
      Rule 3 (priority director). For North at-need, Aaron Beck is preferred
                                 first when he is on shift and free. Lives
                                 in `scheduling.PRIORITY_DIRECTORS`.
      Rule 4 (venue auto-pick).  When `room_calendars_enabled`, pick the
                                 lowest-numbered free venue at the requested
                                 site (PM-1 → PM-2 → ...). The brief uses
                                 anonymous slot codes; the spreadsheet caps
                                 concurrent bookings via venue count, not
                                 named rooms.

    The pre-need flow (`_propose_pre_need`) does NOT participate in any of
    this — pre-arrangers book against their own calendars only, no venue
    cap, no fixed slot grid. See APPOINTMENTS_ARCHITECTURE.md §3.2.
    """

    # Step 1 — read the Primaries roster (events-as-availability, convention A).
    roster_cal = await _single_calendar_of_kind(
        db, organization.id, kind="primaries_roster"
    )
    if roster_cal is None:
        logger.warning("at_need_no_roster org=%s", organization.id)
        return []

    on_shift_names = await _on_shift_primaries(
        calendar, roster_cal.google_id, window_start, window_end
    )
    if not on_shift_names:
        return []

    # Step 2 — split on-shift directors by territory tag.
    region = location_region(location_slug)
    if region == "unknown":
        territory_match: List[str] = list(on_shift_names)
        territory_other: List[str] = []
    else:
        territory_match = filter_counselors_for_region(on_shift_names, region)
        territory_other = [n for n in on_shift_names if n not in territory_match]

    # Step 3 — resolve Primary calendar rows for everyone on shift today
    # (we may need both pools depending on the slot rule).
    primary_cals = await _primary_calendars_by_label(db, organization.id, on_shift_names)
    primary_cals_by_label = {c.name: c for c in primary_cals}
    if not primary_cals_by_label:
        return []

    # Step 4 — venue candidates at this location, lowest-numbered first.
    # Loaded once and reused per slot for the cheapest auto-pick (rule 4).
    venue_cals_at_site: List[Calendar] = []
    if flags.room_calendars_enabled:
        venue_cals_at_site = await _venue_calendars_at_location(
            db, organization.id, location_slug
        )

    # Step 5 — iterate the fixed slot grid in chronological order so we can
    # apply the 12:15/15:00 continuity rule (rule 2) using the previous
    # slot's chosen director.
    proposals: List[SlotProposal] = []
    last_primary_at_site: Optional[str] = None

    for hhmm in available_slots(location_slug):
        try:
            hour, minute = (int(p) for p in hhmm.split(":"))
        except ValueError:
            continue
        start_local = window_start.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        end_local = start_local + duration
        if end_local > window_end:
            continue

        # Build the ordered candidate-director list per slot (rules 1–3).
        eligible_names = _ordered_directors_for_slot(
            hhmm=hhmm,
            region=region,
            territory_match=territory_match,
            territory_other=territory_other,
            last_primary_at_site=last_primary_at_site,
        )
        eligible_cals = [
            primary_cals_by_label[n]
            for n in eligible_names
            if n in primary_cals_by_label
        ]
        if not eligible_cals:
            continue

        chosen_primary = await _first_free_primary(
            calendar, eligible_cals, start_local, end_local
        )
        if chosen_primary is None:
            continue

        # Venue selection (rule 4).
        chosen_venue: Optional[Calendar] = None
        if flags.room_calendars_enabled:
            if not venue_cals_at_site:
                # Flag is on but no venues seeded at this location — fail
                # closed rather than offer a slot we can't actually hold.
                logger.warning(
                    "at_need_no_venues_seeded org=%s location=%s",
                    organization.id,
                    location_slug,
                )
                continue
            chosen_venue = await _first_free_primary(
                calendar, venue_cals_at_site, start_local, end_local
            )
            if chosen_venue is None:
                # All venues at this site are booked at this slot.
                continue
        elif venue_calendar_google_id:
            # Caller pinned a specific venue (e.g. comms-platform manual booker
            # before the room-calendars flag flips). Honour the pin.
            v = await _calendar_by_google_id(
                db, organization.id, venue_calendar_google_id
            )
            if v is None or not await _is_free(
                calendar, v.google_id, start_local, end_local
            ):
                continue
            chosen_venue = v

        proposals.append(
            SlotProposal(
                starts_at=start_local,
                ends_at=end_local,
                primary_calendar_id=chosen_primary.google_id,
                primary_label=chosen_primary.name,
                venue_calendar_id=(chosen_venue.google_id if chosen_venue else None),
                venue_label=(chosen_venue.name if chosen_venue else None),
            )
        )
        last_primary_at_site = chosen_primary.name
        if len(proposals) >= max_slots:
            break

    return proposals


def _ordered_directors_for_slot(
    *,
    hhmm: str,
    region: str,
    territory_match: List[str],
    territory_other: List[str],
    last_primary_at_site: Optional[str],
) -> List[str]:
    """Return directors ordered by preference for a single slot.

    9am (strict territory) — only territory-matching directors are eligible.
    Within the eligible set, priority directors (e.g. Aaron Beck for North
    at-need) move to the front.

    12:15 / 15:00 (continuity) — same-site continuity director first (when
    they're still on shift), then territory-match directors, then any other
    on-shift director as a fallback. Priority directors apply within the
    territory-match group.
    """
    priority = PRIORITY_DIRECTORS.get((region, "at_need"), [])

    if is_strict_territory_slot(hhmm):
        return apply_priority_order(territory_match, priority)

    # 12:15 / 15:00 — continuity first, then territory-match, then other.
    ordered: List[str] = []
    if last_primary_at_site and last_primary_at_site in (
        territory_match + territory_other
    ):
        ordered.append(last_primary_at_site)

    for name in apply_priority_order(territory_match, priority):
        if name not in ordered:
            ordered.append(name)
    for name in territory_other:
        if name not in ordered:
            ordered.append(name)
    return ordered


# ─── Pre-need flow (APPOINTMENTS_ARCHITECTURE.md §3.2) ────────────────────────


async def _propose_pre_need(
    *,
    db: AsyncSession,
    calendar: CalendarClient,
    organization: Organization,
    window_start: datetime,
    window_end: datetime,
    duration: timedelta,
    tz: ZoneInfo,
    max_slots: int,
) -> List[SlotProposal]:
    """Pre-need = book directly against pre-arranger calendars (convention B)."""

    pre_cals = await _calendars_of_kind(db, organization.id, kind="pre_arranger")
    if not pre_cals:
        return []

    candidates = _generic_candidate_starts(window_start, tz)
    proposals: List[SlotProposal] = []
    for start_local in candidates:
        end_local = start_local + duration
        if end_local > window_end:
            continue
        chosen = await _first_free_primary(calendar, pre_cals, start_local, end_local)
        if chosen is None:
            continue
        proposals.append(
            SlotProposal(
                starts_at=start_local,
                ends_at=end_local,
                primary_calendar_id=chosen.google_id,
                primary_label=chosen.name,
            )
        )
        if len(proposals) >= max_slots:
            break

    return proposals


# ─── Calendar reads ───────────────────────────────────────────────────────────


async def _on_shift_primaries(
    calendar: CalendarClient,
    roster_google_id: str,
    window_start: datetime,
    window_end: datetime,
) -> List[str]:
    """Convention A: parse the Primaries roster events to learn who's on shift."""
    events = await calendar.list_events(
        roster_google_id,
        time_min_iso=window_start.isoformat(),
        time_max_iso=window_end.isoformat(),
    )
    names: List[str] = []
    for ev in events:
        name = parse_counselor_from_event(ev.get("summary", ""))
        if name and name not in names:
            names.append(name)
    return names


async def _is_free(
    calendar: CalendarClient,
    google_calendar_id: str,
    start_local: datetime,
    end_local: datetime,
) -> bool:
    """Convention B: free/busy check for a single calendar in a single window."""
    busy = await calendar.free_busy(
        google_calendar_id,
        time_min_iso=start_local.isoformat(),
        time_max_iso=end_local.isoformat(),
        timezone=str(start_local.tzinfo) if start_local.tzinfo else "UTC",
    )
    return not bool(busy)


async def _first_free_primary(
    calendar: CalendarClient,
    primaries: Sequence[Calendar],
    start_local: datetime,
    end_local: datetime,
) -> Optional[Calendar]:
    """Return the first Primary/Pre-arranger calendar with no busy block in the window."""
    for cal in primaries:
        if await _is_free(calendar, cal.google_id, start_local, end_local):
            return cal
    return None


# ─── DB helpers ───────────────────────────────────────────────────────────────


async def _calendars_of_kind(
    db: AsyncSession, organization_id: Any, *, kind: str
) -> List[Calendar]:
    stmt = (
        select(Calendar)
        .where(
            Calendar.organization_id == organization_id,
            Calendar.kind == kind,
            Calendar.active.is_(True),
        )
        .order_by(Calendar.name)
    )
    return list((await db.execute(stmt)).scalars().all())


async def _single_calendar_of_kind(
    db: AsyncSession, organization_id: Any, *, kind: str
) -> Optional[Calendar]:
    cals = await _calendars_of_kind(db, organization_id, kind=kind)
    if not cals:
        return None
    if len(cals) > 1:
        # The roster is meant to be a single shared calendar; warn loudly if
        # the seed data accidentally creates more than one.
        logger.warning(
            "multiple_calendars_for_singleton_kind kind=%s org=%s count=%d",
            kind,
            organization_id,
            len(cals),
        )
    return cals[0]


async def _primary_calendars_by_label(
    db: AsyncSession, organization_id: Any, labels: Iterable[str]
) -> List[Calendar]:
    """Resolve Primary calendars by display name (e.g. 'Ashley R.').

    Requires Calendar.name (or metadata.display_name) to match the roster's
    summary tokens. The seed step in W7 must enforce this naming contract.
    """
    label_set = {l.strip() for l in labels if l and l.strip()}
    if not label_set:
        return []
    stmt = select(Calendar).where(
        Calendar.organization_id == organization_id,
        Calendar.kind == "primary",
        Calendar.active.is_(True),
        Calendar.name.in_(label_set),
    )
    return list((await db.execute(stmt)).scalars().all())


async def _venue_calendars_at_location(
    db: AsyncSession, organization_id: Any, location_slug: str
) -> List[Calendar]:
    """Return active venue calendars at a site, ordered by name.

    Per the venue brief, venue calendars use anonymous codes like `PM-1`,
    `PM-2`, ..., `CH-1`, ... Ordering by `Calendar.name` therefore yields
    "lowest-numbered free venue first" — exactly the auto-pick rule we want.

    Each venue row is expected to carry `metadata.location_slug = '<slug>'`
    set at seed time. If the seed step ever switches to multi-location
    venues (a venue serving more than one site), broaden this query to
    `location_slugs []` and adjust the comparison accordingly.
    """
    # Filter the slug in Python so this query is portable across the
    # Postgres prod backend (JSONB) and the SQLite test backend (JSON).
    # 22 venue rows total — the in-memory filter is free.
    stmt = (
        select(Calendar)
        .where(
            Calendar.organization_id == organization_id,
            Calendar.kind == "venue",
            Calendar.active.is_(True),
        )
        .order_by(Calendar.name)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [r for r in rows if (r.metadata_ or {}).get("location_slug") == location_slug]


async def _calendar_by_google_id(
    db: AsyncSession, organization_id: Any, google_id: str
) -> Optional[Calendar]:
    stmt = select(Calendar).where(
        Calendar.organization_id == organization_id,
        Calendar.google_id == google_id,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _calendar_by_pk(
    db: AsyncSession, calendar_pk: Optional[Any]
) -> Optional[Calendar]:
    if calendar_pk is None:
        return None
    return await db.get(Calendar, calendar_pk)


# ─── Time helpers ─────────────────────────────────────────────────────────────


def _safe_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("America/Edmonton")


def _day_window(target: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime(target.year, target.month, target.day, 0, 0, 0, tzinfo=tz)
    end = datetime(target.year, target.month, target.day, 23, 59, 59, tzinfo=tz)
    return start, end


def _candidate_starts_for(
    location_slug: str, window_start: datetime, tz: ZoneInfo
) -> List[datetime]:
    """Translate the legacy fixed slot grid (09:00 / 12:15 / 15:00) into datetimes."""
    out: List[datetime] = []
    for hhmm in available_slots(location_slug):
        try:
            hour, minute = (int(p) for p in hhmm.split(":"))
        except ValueError:
            continue
        out.append(window_start.replace(hour=hour, minute=minute, second=0, microsecond=0))
    return out


def _generic_candidate_starts(window_start: datetime, tz: ZoneInfo) -> List[datetime]:
    """Hourly grid 09:00–16:00 for pre-need until ops gives us specific slots."""
    out: List[datetime] = []
    for hour in range(9, 17):
        out.append(window_start.replace(hour=hour, minute=0, second=0, microsecond=0))
    return out


# ─── Display helpers ──────────────────────────────────────────────────────────


def _family_label(contact: Contact) -> str:
    """Best-effort family label from the (single-field) Contact.name.

    The Contact ORM exposes only `name` ("First Last"); GHL is the source of
    truth for first/last splits. We try the last token of `name`, falling
    back to the whole name, then "Family" so booking copy is never empty.
    """
    full = (getattr(contact, "name", None) or "").strip()
    if not full:
        return "Family"
    tokens = [t for t in full.split() if t]
    if len(tokens) >= 2:
        return f"{tokens[-1]} Family"
    return f"{tokens[0]} Family"


def _default_summary(service_type: str, family_label: str, slot: SlotProposal) -> str:
    parts = [service_type.replace("_", " ").title(), "—", family_label, "with", slot.primary_label]
    if slot.venue_label:
        parts.extend(["at", slot.venue_label])
    return " ".join(parts)


def _default_description(
    *,
    service_type: str,
    family_label: str,
    slot: SlotProposal,
    notes: Optional[str],
    contact: Contact,
) -> str:
    lines = [
        f"Service Type: {service_type}",
        f"Family: {family_label}",
        f"Primary: {slot.primary_label}",
    ]
    if slot.venue_label:
        lines.append(f"Venue: {slot.venue_label}")
    phone = getattr(contact, "phone", None) or getattr(contact, "phone_e164", None)
    if phone:
        lines.append(f"Contact phone: {phone}")
    email = getattr(contact, "email", None)
    if email:
        lines.append(f"Contact email: {email}")
    if notes:
        lines.append(f"Notes: {notes}")
    lines.append("Booked via Sarah / Comms platform")
    return "\n".join(lines)
