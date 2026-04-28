"""Permanently delete the 3 SA-owned calendars left over from session 11
that the realignment in session 12 made redundant.

Targets (SA-owned, no longer referenced by any sarah.calendars row):
  - MHC - Primaries Roster   (replaced by M&H's existing 5c309c2d3... roster)
  - MHC - Aaron B.           (per-Primary calendars are not used; busy state
  - MHC - McKenzi S.          comes from the shared booking calendar)

Idempotent: re-runs are no-ops if the calendars are already gone.

USAGE
  cd backend
  python -m scripts.delete_dormant_pilot_calendars --dry-run
  python -m scripts.delete_dormant_pilot_calendars
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import List

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import select

from app.config import get_settings
from app.database.session import async_session_factory
from app.models.calendar import Calendar
from app.models.organization import Organization

ORG_SLUG = "mhc"

DORMANT_CALENDAR_IDS = [
    "f0d53806fa2be2e9cfde21ba41b3282adc35732208ae5f54c74f9836f94d2cc1@group.calendar.google.com",
    "f39000b8a3c2030956b07f9ba934c61bf653e6912964f0b36372e9a3c578ac05@group.calendar.google.com",
    "8f0622764ae9fa3f68a275f5226e2247b2c1ae68dd253b015cf825151487639d@group.calendar.google.com",
]

EXPECTED_NAMES = {
    "MHC - Primaries Roster",
    "MHC - Aaron B.",
    "MHC - McKenzi S.",
}


def _build_google_service():
    settings = get_settings()
    raw = (settings.google_calendar_credentials or "").strip()
    if not raw:
        raise RuntimeError("GOOGLE_CALENDAR_CREDENTIALS not set")
    scopes = ["https://www.googleapis.com/auth/calendar"]
    if raw.startswith("{"):
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = service_account.Credentials.from_service_account_file(raw, scopes=scopes)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


async def _ensure_no_active_db_references() -> List[str]:
    """Safety check: refuse to delete a Google calendar that's still referenced
    by an *active* sarah.calendars row. Returns a list of human-readable
    warnings if any reference is found."""
    warnings: List[str] = []
    async with async_session_factory() as db:
        org = (
            await db.execute(select(Organization).where(Organization.slug == ORG_SLUG))
        ).scalar_one()
        rows = (
            await db.execute(
                select(Calendar).where(
                    Calendar.organization_id == org.id,
                    Calendar.google_id.in_(DORMANT_CALENDAR_IDS),
                )
            )
        ).scalars().all()
        for r in rows:
            if r.active:
                warnings.append(
                    f"sarah.calendars row id={r.id} kind={r.kind} name={r.name!r} "
                    f"is still active=True for google_id={r.google_id} — "
                    f"refusing to delete"
                )
    return warnings


def _delete_one(svc, calendar_id: str, dry: bool) -> str:
    """Sanity-check the summary, then DELETE. Returns a status string."""
    try:
        meta = svc.calendars().get(calendarId=calendar_id).execute()
    except HttpError as e:
        if e.resp.status in (404, 410):
            return "already gone"
        return f"FETCH_FAIL {type(e).__name__}: {e}"

    summary = (meta.get("summary") or "").strip()
    if summary not in EXPECTED_NAMES:
        return (
            f"REFUSED — calendar summary {summary!r} is not in the expected "
            f"deletion set {sorted(EXPECTED_NAMES)}; aborting to avoid a "
            "wrong-target delete."
        )

    if dry:
        return f"DRY: would DELETE {calendar_id}  ({summary!r})"

    try:
        svc.calendars().delete(calendarId=calendar_id).execute()
    except HttpError as e:
        return f"DELETE_FAIL {type(e).__name__}: {e}"
    return f"deleted {summary!r}"


async def _purge_inactive_db_rows(dry: bool) -> int:
    """Drop the inactive sarah.calendars rows whose google_id we just nuked."""
    async with async_session_factory() as db:
        org = (
            await db.execute(select(Organization).where(Organization.slug == ORG_SLUG))
        ).scalar_one()
        rows = (
            await db.execute(
                select(Calendar).where(
                    Calendar.organization_id == org.id,
                    Calendar.google_id.in_(DORMANT_CALENDAR_IDS),
                    Calendar.active.is_(False),
                )
            )
        ).scalars().all()
        if not rows:
            print("\n  (no inactive DB rows to purge)")
            return 0
        for r in rows:
            print(f"  purge: id={r.id} kind={r.kind} name={r.name!r}")
            if not dry:
                await db.delete(r)
        if not dry:
            await db.commit()
        return len(rows)


async def main(dry: bool) -> int:
    print(f"DRY RUN: {dry}")

    warnings = await _ensure_no_active_db_references()
    if warnings:
        print("\nABORTING — found active DB references:")
        for w in warnings:
            print(f"  - {w}")
        return 1

    svc = _build_google_service()
    print(f"\nDeleting {len(DORMANT_CALENDAR_IDS)} dormant SA-owned calendars:")
    for cid in DORMANT_CALENDAR_IDS:
        status = _delete_one(svc, cid, dry)
        print(f"  - {cid}\n      {status}")

    print("\nPurging inactive sarah.calendars rows:")
    n = await _purge_inactive_db_rows(dry)
    print(f"  total: {n}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.dry_run)))
