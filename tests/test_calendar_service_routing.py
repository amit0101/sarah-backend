"""Routing-rule tests for `_propose_at_need` (mh_venues_brief.html, 2026-04-25).

We monkeypatch `calendar_service`'s DB helpers to return canned Calendar
stand-ins, so these tests exercise the routing logic only — no SQLAlchemy
schema needed (the existing test infra has no JSONB→JSON shim).

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
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from app.services import calendar_service as cal_svc


# ─── Fakes ─────────────────────────────────────────────────────────────────────


@dataclass
class FakeCal:
    """Stand-in for app.models.calendar.Calendar — only the fields the
    routing code reads."""

    name: str
    google_id: str
    kind: str = "primary"
    metadata_: Dict[str, Any] = None  # noqa: RUF012

    def __post_init__(self) -> None:
        if self.metadata_ is None:
            self.metadata_ = {}


def _roster_event(name: str) -> Dict[str, Any]:
    return {"summary": f"Primaries - {name} - 8:45 AM to 5:15 PM"}


class FakeCalendarClient:
    """Mocked Google adapter.

    `roster_names`     — names returned by list_events on the roster cal.
    `busy_calendars`   — google_ids that report busy regardless of window.
    `partial_busy`     — fn(google_id, start_iso) -> bool, finer control.
    """

    def __init__(
        self,
        *,
        roster_google_id: str,
        roster_names: List[str],
        busy_calendars: Optional[set] = None,
        partial_busy=None,
    ) -> None:
        self.roster_google_id = roster_google_id
        self.roster_names = roster_names
        self.busy_calendars = busy_calendars or set()
        self.partial_busy = partial_busy
        self.create_event = AsyncMock(return_value={"id": "evt-1"})
        self.update_event = AsyncMock(return_value={"id": "evt-1"})
        self.delete_event = AsyncMock(return_value=None)

    async def list_events(self, calendar_id, *, time_min_iso, time_max_iso):
        if calendar_id == self.roster_google_id:
            return [_roster_event(n) for n in self.roster_names]
        return []

    async def free_busy(self, calendar_id, *, time_min_iso, time_max_iso, timezone):
        if calendar_id in self.busy_calendars:
            return [{"start": time_min_iso, "end": time_max_iso}]
        if self.partial_busy and self.partial_busy(calendar_id, time_min_iso):
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
    roster_google_id: str,
    primaries: List[FakeCal],
    venues_by_site: Optional[Dict[str, List[FakeCal]]] = None,
):
    """Stub the SQLAlchemy helpers so no DB session is needed."""
    venues_by_site = venues_by_site or {}

    roster = FakeCal(name="Roster", google_id=roster_google_id, kind="primaries_roster")

    async def fake_single_of_kind(db, org_id, *, kind):
        return roster if kind == "primaries_roster" else None

    async def fake_primary_by_label(db, org_id, labels):
        wanted = {l.strip() for l in labels}
        return [p for p in primaries if p.name in wanted]

    async def fake_venues_at_location(db, org_id, location_slug):
        return venues_by_site.get(location_slug, [])

    monkeypatch.setattr(cal_svc, "_single_calendar_of_kind", fake_single_of_kind)
    monkeypatch.setattr(cal_svc, "_primary_calendars_by_label", fake_primary_by_label)
    monkeypatch.setattr(cal_svc, "_venue_calendars_at_location", fake_venues_at_location)


def _make_primaries(*names: str) -> List[FakeCal]:
    return [
        FakeCal(
            name=n,
            google_id=f"primary-{n.replace(' ', '_').replace('.', '').lower()}@mh",
        )
        for n in names
    ]


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


# ─── R3 — Aaron Beck priority for North 9am at-need ────────────────────────────


@pytest.mark.asyncio
async def test_north_9am_prefers_aaron_beck_when_on_shift(monkeypatch, fake_org):
    primaries = _make_primaries("Terra S.", "Aaron B.", "Ashley R.")
    venues = _make_venues("chapel_of_the_bells", "CH-1", "CH-2", "CH-3")
    _patch_db_helpers(
        monkeypatch,
        roster_google_id="roster@mh",
        primaries=primaries,
        venues_by_site={"chapel_of_the_bells": venues},
    )
    cal = FakeCalendarClient(
        roster_google_id="roster@mh",
        roster_names=["Terra S.", "Aaron B.", "Ashley R."],
    )

    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
    )
    assert slots
    nine = next(s for s in slots if s.starts_at.hour == 9)
    assert nine.primary_label == "Aaron B."


@pytest.mark.asyncio
async def test_north_9am_falls_back_when_aaron_off_shift(monkeypatch, fake_org):
    primaries = _make_primaries("Terra S.", "Ashley R.")
    venues = _make_venues("chapel_of_the_bells", "CH-1")
    _patch_db_helpers(
        monkeypatch,
        roster_google_id="roster@mh",
        primaries=primaries,
        venues_by_site={"chapel_of_the_bells": venues},
    )
    cal = FakeCalendarClient(
        roster_google_id="roster@mh",
        roster_names=["Terra S.", "Ashley R."],
    )

    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
    )
    nine = next(s for s in slots if s.starts_at.hour == 9)
    assert nine.primary_label in {"Terra S.", "Ashley R."}


@pytest.mark.asyncio
async def test_north_9am_skips_aaron_when_busy(monkeypatch, fake_org):
    primaries = _make_primaries("Terra S.", "Aaron B.", "Ashley R.")
    venues = _make_venues("chapel_of_the_bells", "CH-1")
    _patch_db_helpers(
        monkeypatch,
        roster_google_id="roster@mh",
        primaries=primaries,
        venues_by_site={"chapel_of_the_bells": venues},
    )
    aaron_id = next(p.google_id for p in primaries if p.name == "Aaron B.")
    cal = FakeCalendarClient(
        roster_google_id="roster@mh",
        roster_names=["Terra S.", "Aaron B.", "Ashley R."],
        busy_calendars={aaron_id},
    )

    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
    )
    nine = next(s for s in slots if s.starts_at.hour == 9)
    assert nine.primary_label == "Terra S."


# ─── R1 — 9am territory hard-match ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_south_9am_excludes_north_directors(monkeypatch, fake_org):
    """South-territory site at 9am must not offer a North-tagged director."""
    primaries = _make_primaries("Aaron B.", "McKenzi S.", "Jillian G.")
    venues = _make_venues("park_memorial", "PM-1", "PM-2", "PM-3", "PM-4")
    _patch_db_helpers(
        monkeypatch,
        roster_google_id="roster@mh",
        primaries=primaries,
        venues_by_site={"park_memorial": venues},
    )
    cal = FakeCalendarClient(
        roster_google_id="roster@mh",
        roster_names=["Aaron B.", "McKenzi S.", "Jillian G."],
    )

    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="park_memorial",
        target_date=date(2026, 5, 4),
    )
    nine = next(s for s in slots if s.starts_at.hour == 9)
    assert nine.primary_label in {"McKenzi S.", "Jillian G."}
    assert nine.primary_label != "Aaron B."


# ─── R2 — 12:15 / 15:00 same-site continuity ──────────────────────────────────


@pytest.mark.asyncio
async def test_continuity_keeps_same_director_across_slots(monkeypatch, fake_org):
    primaries = _make_primaries("Aaron B.", "Terra S.", "Ashley R.")
    venues = _make_venues("chapel_of_the_bells", "CH-1", "CH-2")
    _patch_db_helpers(
        monkeypatch,
        roster_google_id="roster@mh",
        primaries=primaries,
        venues_by_site={"chapel_of_the_bells": venues},
    )
    cal = FakeCalendarClient(
        roster_google_id="roster@mh",
        roster_names=["Aaron B.", "Terra S.", "Ashley R."],
    )

    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
    )
    by_hour = {s.starts_at.hour: s.primary_label for s in slots}
    assert by_hour[9] == "Aaron B."
    assert by_hour[12] == "Aaron B."  # continuity
    assert by_hour[15] == "Aaron B."  # continuity


@pytest.mark.asyncio
async def test_1215_falls_back_when_continuity_director_busy(monkeypatch, fake_org):
    primaries = _make_primaries("Aaron B.", "Terra S.")
    venues = _make_venues("chapel_of_the_bells", "CH-1", "CH-2")
    _patch_db_helpers(
        monkeypatch,
        roster_google_id="roster@mh",
        primaries=primaries,
        venues_by_site={"chapel_of_the_bells": venues},
    )
    aaron_id = next(p.google_id for p in primaries if p.name == "Aaron B.")

    def aaron_busy_only_at_1215(google_id, start_iso):
        return google_id == aaron_id and "T12:15" in start_iso

    cal = FakeCalendarClient(
        roster_google_id="roster@mh",
        roster_names=["Aaron B.", "Terra S."],
        partial_busy=aaron_busy_only_at_1215,
    )

    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
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
    primaries = _make_primaries("Aaron B.")
    venues = _make_venues("chapel_of_the_bells", "CH-1", "CH-2", "CH-3")
    _patch_db_helpers(
        monkeypatch,
        roster_google_id="roster@mh",
        primaries=primaries,
        venues_by_site={"chapel_of_the_bells": venues},
    )
    cal = FakeCalendarClient(
        roster_google_id="roster@mh",
        roster_names=["Aaron B."],
    )
    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
    )
    assert slots[0].venue_label == "CH-1"


@pytest.mark.asyncio
async def test_venue_autopick_skips_busy_to_next(monkeypatch, fake_org):
    primaries = _make_primaries("Aaron B.")
    venues = _make_venues("chapel_of_the_bells", "CH-1", "CH-2", "CH-3")
    _patch_db_helpers(
        monkeypatch,
        roster_google_id="roster@mh",
        primaries=primaries,
        venues_by_site={"chapel_of_the_bells": venues},
    )
    ch1_id = next(v.google_id for v in venues if v.name == "CH-1")
    cal = FakeCalendarClient(
        roster_google_id="roster@mh",
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
    )
    assert slots[0].venue_label == "CH-2"


@pytest.mark.asyncio
async def test_venue_autopick_skipped_when_flag_off(monkeypatch, fake_org_no_rooms):
    primaries = _make_primaries("Aaron B.")
    venues = _make_venues("chapel_of_the_bells", "CH-1")
    _patch_db_helpers(
        monkeypatch,
        roster_google_id="roster@mh",
        primaries=primaries,
        venues_by_site={"chapel_of_the_bells": venues},
    )
    cal = FakeCalendarClient(
        roster_google_id="roster@mh",
        roster_names=["Aaron B."],
    )
    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org_no_rooms,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
    )
    assert slots
    for s in slots:
        assert s.venue_calendar_id is None
        assert s.venue_label is None


@pytest.mark.asyncio
async def test_returns_empty_when_no_directors_on_shift(monkeypatch, fake_org):
    primaries = _make_primaries("Aaron B.")
    venues = _make_venues("chapel_of_the_bells", "CH-1")
    _patch_db_helpers(
        monkeypatch,
        roster_google_id="roster@mh",
        primaries=primaries,
        venues_by_site={"chapel_of_the_bells": venues},
    )
    cal = FakeCalendarClient(roster_google_id="roster@mh", roster_names=[])
    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
    )
    assert slots == []


@pytest.mark.asyncio
async def test_returns_empty_when_flag_on_but_no_venues_seeded(monkeypatch, fake_org):
    """Brief: when the flag is on we fail closed if no venues exist at the site."""
    primaries = _make_primaries("Aaron B.")
    _patch_db_helpers(
        monkeypatch,
        roster_google_id="roster@mh",
        primaries=primaries,
        venues_by_site={},  # nothing seeded
    )
    cal = FakeCalendarClient(roster_google_id="roster@mh", roster_names=["Aaron B."])
    slots = await cal_svc.propose_slots(
        db=None,
        calendar=cal,
        organization=fake_org,
        intent="at_need",
        location_slug="chapel_of_the_bells",
        target_date=date(2026, 5, 4),
    )
    assert slots == []
