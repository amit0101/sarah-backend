"""Pilot smoke test: probe `calendar_service.propose_slots` directly.

USAGE
  cd backend
  python -m scripts.smoke_propose_slots                                 # default: tomorrow Edmonton
  python -m scripts.smoke_propose_slots --date 2026-04-29
  python -m scripts.smoke_propose_slots --location park_memorial

Bypasses the LLM. Hits the same code path Sarah's `_check_calendar`
takes when `_has_seeded_primaries` returns True. Verifies R1/R3/R4 of
the routing rules in `APPOINTMENTS_ARCHITECTURE.md` §4.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date as date_cls, datetime, timedelta
from typing import List
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.calendar_client.google_adapter import GoogleCalendarAdapter
from app.database.session import async_session_factory
from app.models.location import Location
from app.models.organization import Organization
from app.services import calendar_service as cal_svc

ORG_SLUG = "mhc"
TIMEZONE = "America/Edmonton"

DEFAULT_LOCATIONS = ["park_memorial", "chapel_of_the_bells"]


async def probe(target: date_cls, locations: List[str]) -> int:
    cal = GoogleCalendarAdapter()
    async with async_session_factory() as db:
        org = (
            await db.execute(select(Organization).where(Organization.slug == ORG_SLUG))
        ).scalar_one_or_none()
        if org is None:
            print(f"FAIL: organization '{ORG_SLUG}' not found")
            return 1

        flags = (org.config or {}).get("feature_flags") or {}
        print(f"organization      : {org.slug}  id={org.id}")
        print(f"feature_flags     : {flags}")
        print(f"target_date       : {target.isoformat()}  ({TIMEZONE})")
        print()

        for slug in locations:
            print(f"── {slug} " + "─" * (60 - len(slug)))
            location = await db.get(Location, (org.id, slug))
            booking_cal_id = location.calendar_id if location else None
            print(f"  booking_calendar  : {booking_cal_id}")
            try:
                slots = await cal_svc.propose_slots(
                    db=db,
                    calendar=cal,
                    organization=org,
                    intent="at_need",
                    location_slug=slug,
                    target_date=target,
                    timezone=TIMEZONE,
                    booking_calendar_google_id=booking_cal_id,
                )
            except Exception as e:
                print(f"  ERROR: {type(e).__name__}: {e}")
                continue

            if not slots:
                print("  (no slots returned)")
                continue

            print(f"  {len(slots)} slot(s):")
            for i, s in enumerate(slots, 1):
                primary_label = getattr(s, "primary_label", None) or "(no name)"
                venue_label = getattr(s, "venue_label", None) or "—"
                start_local = s.starts_at.astimezone(ZoneInfo(TIMEZONE))
                end_local = s.ends_at.astimezone(ZoneInfo(TIMEZONE))
                print(
                    f"    [{i}] {start_local.strftime('%a %b %d %H:%M')}–"
                    f"{end_local.strftime('%H:%M')}  "
                    f"primary={primary_label}  venue={venue_label}"
                )
                print(
                    f"        primary_cal_id={s.primary_calendar_id}"
                )
                if s.venue_calendar_id:
                    print(f"        venue_cal_id  ={s.venue_calendar_id}")
            print()
    return 0


def _parse_date(s: str) -> date_cls:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _default_target_date() -> date_cls:
    return (datetime.now(ZoneInfo(TIMEZONE)) + timedelta(days=1)).date()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Probe propose_slots() directly.")
    p.add_argument("--date", type=_parse_date, default=None, help="YYYY-MM-DD Edmonton.")
    p.add_argument(
        "--location",
        action="append",
        default=None,
        help="Location slug (repeatable). Default: park_memorial + chapel_of_the_bells.",
    )
    args = p.parse_args()
    target = args.date or _default_target_date()
    locs = args.location or DEFAULT_LOCATIONS
    sys.exit(asyncio.run(probe(target, locs)))
