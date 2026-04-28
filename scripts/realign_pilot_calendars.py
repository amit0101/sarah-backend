"""Realign sarah.calendars rows to M&H's actual operating model.

Background
----------
The session-11 pilot provisioning created three SA-owned calendars that turn
out to be redundant given M&H's existing infrastructure:

  - "MHC - Primaries Roster"  → M&H already has a shared "Primaries" roster
                                (`5c309c2d3...@group.calendar.google.com`).
                                Use that instead of a parallel SA copy.

  - "MHC - Aaron B."          → M&H writes Primary bookings to
  - "MHC - McKenzi S."          j.hagel@mhfh.com with the director name in
                                the event title. We don't need per-director
                                calendars — busy state is derived by parsing
                                event titles. Drop these rows.

The 4 venue calendars (PM-1, PM-2, CH-1, CH-2) are correct and stay.

This script makes the database changes only. The Google Calendar resources
themselves are deactivated, not deleted (operator can clean up later).

USAGE
  cd backend
  python -m scripts.realign_pilot_calendars --dry-run
  python -m scripts.realign_pilot_calendars
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select, text

from app.database.session import async_session_factory
from app.models.calendar import Calendar
from app.models.organization import Organization

ORG_SLUG = "mhc"

EXISTING_PRIMARIES_ROSTER_GOOGLE_ID = (
    "5c309c2d3cbfaaae6da6619cac2fe8d6e32392c503ab231c3813512ddc54f662"
    "@group.calendar.google.com"
)


async def _resolve_org_id(db) -> str:
    org = (
        await db.execute(select(Organization).where(Organization.slug == ORG_SLUG))
    ).scalar_one_or_none()
    if org is None:
        raise RuntimeError(f"organization slug={ORG_SLUG} not found")
    return org.id


async def main(dry: bool) -> int:
    print(f"DRY RUN: {dry}")
    async with async_session_factory() as db:
        org_id = await _resolve_org_id(db)
        print(f"Organization {ORG_SLUG} → {org_id}")

        # Re-point the primaries_roster row to M&H's existing shared roster.
        roster_rows = (
            await db.execute(
                select(Calendar).where(
                    Calendar.organization_id == org_id,
                    Calendar.kind == "primaries_roster",
                )
            )
        ).scalars().all()
        print(f"\nprimaries_roster rows: {len(roster_rows)}")
        for r in roster_rows:
            print(f"  before: id={r.id} google_id={r.google_id} active={r.active}")

        if dry:
            print(f"  DRY: would point {len(roster_rows)} row(s) to {EXISTING_PRIMARIES_ROSTER_GOOGLE_ID}")
        else:
            for r in roster_rows:
                r.google_id = EXISTING_PRIMARIES_ROSTER_GOOGLE_ID
                r.name = "MHC - Primaries (shared)"
                r.active = True
                meta = dict(r.metadata_ or {})
                meta["realigned_to_existing_mh_roster"] = True
                r.metadata_ = meta

        # Deactivate per-Primary rows (Aaron B., McKenzi S.).
        primary_rows = (
            await db.execute(
                select(Calendar).where(
                    Calendar.organization_id == org_id,
                    Calendar.kind == "primary",
                )
            )
        ).scalars().all()
        print(f"\nkind='primary' rows to deactivate: {len(primary_rows)}")
        for p in primary_rows:
            print(f"  before: id={p.id} name={p.name!r} active={p.active}")

        if not dry:
            for p in primary_rows:
                p.active = False
                meta = dict(p.metadata_ or {})
                meta["deactivated_reason"] = (
                    "M&H operates a shared director-bookings calendar; per-director "
                    "rows are not used. See APPOINTMENTS_ARCHITECTURE.md and the "
                    "session-12 realignment notes."
                )
                p.metadata_ = meta

        if dry:
            print("\n(dry run — no changes committed)")
            return 0

        await db.commit()

        # Verify
        rows = (
            await db.execute(
                select(Calendar).where(
                    Calendar.organization_id == org_id,
                    Calendar.active.is_(True),
                ).order_by(Calendar.kind, Calendar.name)
            )
        ).scalars().all()
        print("\nActive calendars after realignment:")
        for r in rows:
            print(f"  {r.kind:18s} {r.name!r:40s} {r.google_id}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.dry_run)))
