"""Routing-rule tests for `_propose_at_need` (mh_venues_brief.html, 2026-04-25).

We monkeypatch `calendar_service`'s DB helpers to return canned Calendar
stand-ins, so these tests exercise the routing logic only — no SQLAlchemy
schema needed (the existing test infra has no JSONB→JSON shim).

Director busy state is driven by events on a single shared booking calendar
(M&H's operating model); the FakeCalendarClient lets each test post events
keyed by director name and slot time.

Rules under test:
  R1  9am bookings filter to directors whose territory tag matches the site.
  R2  12:15 / 15:00 prefer same-site continuity from the previous slot.
  R3  Aaron Beck is offered first for North at-need 9am when on shift + free.
  R4  Venue auto-pick takes the lowest-numbered free venue at the site.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Set
from unittest.mock import AsyncMock

import pytest

from app.services import calendar_service as cal_svc


BOOKING_CAL_ID = "bookings@mh"
ROSTER_CAL_ID = "roster@mh"


# ─── Fakes ─────────────────────────────────────────────────────────────────────


@dataclass
class FakeCal:
    """Stand-in for app.models.calendar.Calendar — only the fields the
    routing code reads."""

    name: str
    google_id: str
    kind: str = "venue"
    metadata_: Dict[str, Any] = None  # noqa: RUF012

    def __post_init__(self) -> None:
        if self.metadata_ is None:
            self.metadata_ = {}


def _roster_event(name: str) -> Dict[str, Any]:
    return {"summary": f"Primaries - {name} - 8:45 AM to 5:15 PM"}


def _booking_event(director: str, hhmm: str = "09:00") -> Dict[str, Any]:
    """Synthesise a Sarah-style booking event title that names the director."""
    return {
        "summary": f"Arrangement — Smith Family with {director} at Chapel",
        "start": {"dateTime": f"2026-05-04T{hhmm}:00-06:00"},
        "end": {"dateTime": f"2026-05-04T{hhmm}:00-06:00"},
    }


class FakeCalendarClient:
    """Mocked Google adapter.

    Args:
      roster_names      Names returned by list_events on the roster cal.
      booking_events    fn(start_iso) -> list of booking events for the
                        slot starting at start_iso. Used to drive
                        per-director busy state by name match.
      busy_calendars    google_ids returning busy from free_busy. Used by
                        venue / pre-arranger calendars (not Primaries).
    """

    def __init__(
        self,
        *,
        roster_names: List[str],
        booking_events: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
        busy_calendars: Optional[Set[str]] = None,
    ) -> None:
        self.roster_names = roster_names
        self.booking_events = booking_events or (lambda _start: [])
        self.busy_calendars = busy_calendars or set()
        self.create_event = AsyncMock(return_value={"id": "evt-1"})
        self.update_event = AsyncMock(return_value={"id": "evt-1"})
        self.delete_event = AsyncMock(return_value=None)

    async def list_events(self, calendar_id, *, time_min_iso, time_max_iso):
        if calendar_id == ROSTER_CAL_ID:
            return [_roster_event(n) for n in self.roster_names]
        if calendar_id == BOOKING_CAL_ID:
            return self.booking_events(time_min_iso)
        return []

    async def free_busy(self, calendar_id, *, time_min_iso, time_max_iso, timezone):
        if calendar_id in self.busy_calendars:
            return [{"start": time_min_iso, "end": time_max_iso}]
        return []


@pytest.fixture
def fake_org():
    """Organization stand-in with room_calendars_enabled=True."""
    return SimpleNamespace(
        id=uuid.UUID("00000000-0000-0000-0000-0000000000a1"),
        config={"feature_flags": {"room_calendars_enabled": True}},
    )


@pytest.fixture
def fake_org_no_rooms():
    return SimpleNamespace(
        id=uuid.UUID("00000000-0000-0000-0000-0000000000a2"),
        config={},
    )


def _patch_db_helpers(
    monkeypatch,
    *,
    venues_by_site: Optional[Dict[str, List[FakeCal]]] = None,
):
    """Stub the SQLAlchemy helpers so no DB session is needed."""
    venues_by_site = venues_by_site or {}

    roster = FakeCal(
        name="Roster", google_id=ROSTER_CAL_ID, kind="primaries_roster"
    )

    async def fake_single_of_kind(db, org_id, *, kind):
        return roster if kind == "primaries_roster" else None

    async def fake_venues_at_location(db, org_id, location_slug):
        return venues_by_site.get(location_slug, [])

    monkeypatch.setattr(cal_svc, "_single_calendar_of_kind", fake_single_of_kind)
    monkeypatch.setattr(cal_svc, "_venue_calendars_at_location", fake_venues_at_location)


def _make_venues(slug: str, *codes: str) -> List[FakeCal]:
    return [
        FakeCal(
            name=c,
            google_id=f"venue-{c.lower()}@mh",
            kind="venue",
            metadata_={"location_slug": slug},
        )
        for c in codes
    ]


def _busy_at(hhmm: str, *directors: str) -> Callable[[str], List[Dict[str, Any]]]:
    """Return a booking_events callable that posts a busy event for each
    director when the slot start matches `hhmm`."""

    def _events(start_iso: str) -> List[Dict[str, Any]]:
        if f"T{hhmm}:" in start_iso:
            return [_booking_event(d, hhmm=hhmm) for d in directors]
        return []

    return _events


def _always_busy(*directors: str) -> Callable[[str], List[Dict[str, Any]]]:
    def _events(_start_iso: str) -> List[Dict[str, Any]]:
        return [_booking_event(d) for d in directors]

    return _events


# ─── R3 — Aaron Beck priority for North 9am at-need ────────────────────────────


@pytest.mark.asyncio
async def test_north_9am_prefers_aaron_beck_when_on_shift(monkeypatch, fake_org):
    venues = _make_venues("chapel_of_the_bells", "CH-1", "CH-2", "CH-3")
    _patch_db_helpers(
        monkeypatch,
        venues_by_site={"chapel_of_the_bells": venues},
    )
    cal = FakeCalendarClient(
        roster_names=["Terra S.", "Aaron B.", "Ashley R."],
    )

    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
        booking_calendar_google_id=BOOKING_CAL_ID,
    )
    assert slots
    nine = next(s for s in slots if s.starts_at.hour == 9)
    assert nine.primary_label == "Aaron B."
    # Bookings are written to the shared calendar, not per-director:
    assert nine.primary_calendar_id == BOOKING_CAL_ID


@pytest.mark.asyncio
async def test_north_9am_falls_back_when_aaron_off_shift(monkeypatch, fake_org):
    venues = _make_venues("chapel_of_the_bells", "CH-1")
    _patch_db_helpers(
        monkeypatch,
        venues_by_site={"chapel_of_the_bells": venues},
    )
    cal = FakeCalendarClient(roster_names=["Terra S.", "Ashley R."])

    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
        booking_calendar_google_id=BOOKING_CAL_ID,
    )
    nine = next(s for s in slots if s.starts_at.hour == 9)
    assert nine.primary_label in {"Terra S.", "Ashley R."}


@pytest.mark.asyncio
async def test_north_9am_skips_aaron_when_busy(monkeypatch, fake_org):
    venues = _make_venues("chapel_of_the_bells", "CH-1")
    _patch_db_helpers(
        monkeypatch,
        venues_by_site={"chapel_of_the_bells": venues},
    )
    cal = FakeCalendarClient(
        roster_names=["Terra S.", "Aaron B.", "Ashley R."],
        booking_events=_always_busy("Aaron B."),
    )

    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
        booking_calendar_google_id=BOOKING_CAL_ID,
    )
    nine = next(s for s in slots if s.starts_at.hour == 9)
    assert nine.primary_label == "Terra S."


# ─── R1 — 9am territory hard-match ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_south_9am_excludes_north_directors(monkeypatch, fake_org):
    """South-territory site at 9am must not offer a North-tagged director."""
    venues = _make_venues("park_memorial", "PM-1", "PM-2", "PM-3", "PM-4")
    _patch_db_helpers(
        monkeypatch,
        venues_by_site={"park_memorial": venues},
    )
    cal = FakeCalendarClient(
        roster_names=["Aaron B.", "McKenzi S.", "Jillian G."],
    )

    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="park_memorial",
        target_date=date(2026, 5, 4),
        booking_calendar_google_id=BOOKING_CAL_ID,
    )
    nine = next(s for s in slots if s.starts_at.hour == 9)
    assert nine.primary_label in {"McKenzi S.", "Jillian G."}
    assert nine.primary_label != "Aaron B."


# ─── R2 — 12:15 / 15:00 same-site continuity ──────────────────────────────────


@pytest.mark.asyncio
async def test_continuity_keeps_same_director_across_slots(monkeypatch, fake_org):
    venues = _make_venues("chapel_of_the_bells", "CH-1", "CH-2")
    _patch_db_helpers(
        monkeypatch,
        venues_by_site={"chapel_of_the_bells": venues},
    )
    cal = FakeCalendarClient(roster_names=["Aaron B.", "Terra S.", "Ashley R."])

    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
        booking_calendar_google_id=BOOKING_CAL_ID,
    )
    by_hour = {s.starts_at.hour: s.primary_label for s in slots}
    assert by_hour[9] == "Aaron B."
    assert by_hour[12] == "Aaron B."  # continuity
    assert by_hour[15] == "Aaron B."  # continuity


@pytest.mark.asyncio
async def test_1215_falls_back_when_continuity_director_busy(monkeypatch, fake_org):
    venues = _make_venues("chapel_of_the_bells", "CH-1", "CH-2")
    _patch_db_helpers(
        monkeypatch,
        venues_by_site={"chapel_of_the_bells": venues},
    )

    cal = FakeCalendarClient(
        roster_names=["Aaron B.", "Terra S."],
        booking_events=_busy_at("12:15", "Aaron B."),
    )

    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
        booking_calendar_google_id=BOOKING_CAL_ID,
    )
    by_hour = {s.starts_at.hour: s.primary_label for s in slots}
    assert by_hour[9] == "Aaron B."
    assert by_hour[12] == "Terra S."  # Aaron busy → fall back
    # 15:00: Terra was the last director at this site at 12:15; the
    # continuity rule keeps her on for 15:00 even though Aaron (priority
    # director for North) is free again. Continuity > priority for
    # non-9am slots — see _ordered_directors_for_slot.
    assert by_hour[15] == "Terra S."


# ─── R4 — Venue auto-pick (lowest-numbered free) ──────────────────────────────


@pytest.mark.asyncio
async def test_venue_autopick_lowest_numbered_free(monkeypatch, fake_org):
    venues = _make_venues("chapel_of_the_bells", "CH-1", "CH-2", "CH-3")
    _patch_db_helpers(
        monkeypatch,
        venues_by_site={"chapel_of_the_bells": venues},
    )
    cal = FakeCalendarClient(roster_names=["Aaron B."])
    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
        booking_calendar_google_id=BOOKING_CAL_ID,
    )
    assert slots[0].venue_label == "CH-1"


@pytest.mark.asyncio
async def test_venue_autopick_skips_busy_to_next(monkeypatch, fake_org):
    venues = _make_venues("chapel_of_the_bells", "CH-1", "CH-2", "CH-3")
    _patch_db_helpers(
        monkeypatch,
        venues_by_site={"chapel_of_the_bells": venues},
    )
    ch1_id = next(v.google_id for v in venues if v.name == "CH-1")
    cal = FakeCalendarClient(
        roster_names=["Aaron B."],
        busy_calendars={ch1_id},
    )
    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
        booking_calendar_google_id=BOOKING_CAL_ID,
    )
    assert slots[0].venue_label == "CH-2"


@pytest.mark.asyncio
async def test_venue_autopick_skipped_when_flag_off(monkeypatch, fake_org_no_rooms):
    venues = _make_venues("chapel_of_the_bells", "CH-1")
    _patch_db_helpers(
        monkeypatch,
        venues_by_site={"chapel_of_the_bells": venues},
    )
    cal = FakeCalendarClient(roster_names=["Aaron B."])
    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org_no_rooms,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
        booking_calendar_google_id=BOOKING_CAL_ID,
    )
    assert slots
    for s in slots:
        assert s.venue_calendar_id is None
        assert s.venue_label is None


@pytest.mark.asyncio
async def test_returns_empty_when_no_directors_on_shift(monkeypatch, fake_org):
    _patch_db_helpers(
        monkeypatch,
        venues_by_site={"chapel_of_the_bells": _make_venues("chapel_of_the_bells", "CH-1")},
    )
    cal = FakeCalendarClient(roster_names=[])
    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
        booking_calendar_google_id=BOOKING_CAL_ID,
    )
    assert slots == []


@pytest.mark.asyncio
async def test_returns_empty_when_flag_on_but_no_venues_seeded(monkeypatch, fake_org):
    """Brief: when the flag is on we fail closed if no venues exist at the site."""
    _patch_db_helpers(
        monkeypatch,
        venues_by_site={},  # nothing seeded
    )
    cal = FakeCalendarClient(roster_names=["Aaron B."])
    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
        booking_calendar_google_id=BOOKING_CAL_ID,
    )
    assert slots == []


@pytest.mark.asyncio
async def test_returns_empty_when_no_booking_calendar(monkeypatch, fake_org):
    """Without a shared booking calendar id, the at-need flow returns []."""
    _patch_db_helpers(
        monkeypatch,
        venues_by_site={"chapel_of_the_bells": _make_venues("chapel_of_the_bells", "CH-1")},
    )
    cal = FakeCalendarClient(roster_names=["Aaron B."])
    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
        # no booking_calendar_google_id
    )
    assert slots == []
