"""Pilot: drop roster events into the `MHC - Primaries Roster` calendar via
the service account. NEVER through a human's Google Calendar UI.

USAGE
  cd backend
  python -m scripts.seed_pilot_roster_events                 # tomorrow Edmonton, both pilot primaries
  python -m scripts.seed_pilot_roster_events --date 2026-04-29
  python -m scripts.seed_pilot_roster_events --dry-run
  python -m scripts.seed_pilot_roster_events --date 2026-04-29 --primary "Aaron B."   # one only

WHY THIS SCRIPT EXISTS
  Per the appointments architecture (and Jeff's confirmed write model), all
  calendar mutations go through the backend's service account. Humans never
  edit the roster (or any pilot calendar) by hand. The eventual production
  surface is the Comms Calendar Management page; this script is the operator
  bridge until that ships.

WHAT IT DOES (idempotent — safe to re-run):
  1. Loads the `MHC - Primaries Roster` row from sarah.calendars (kind=
     'primaries_roster') for the `mhc` org. Fails closed if missing.
  2. Lists events on the roster calendar in the target-date window
     (00:00–23:59 America/Edmonton).
  3. For each entry in `ROSTER_MANIFEST` (default: Aaron B., McKenzi S.):
       a. If a matching event (summary == expected title, same date) already
          exists, skip.
       b. Otherwise insert an event with the exact title required by the
          parser (`scheduling.parse_counselor_from_event`):
             'Primaries - <Name> - 8:45 AM to 5:15 PM'
          Start/end window is 09:00–17:00 America/Edmonton (the visible
          working hours; the 8:45/5:15 in the title is a label-only quirk
          inherited from Jeff's spreadsheet).
  4. Prints a summary table.

The roster regex is `Primaries\s*-\s*(.+?)\s*-\s*\d`, so as long as the title
starts with `Primaries - <Name> -` followed by a digit, the parser is happy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date as date_cls, datetime, time, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from sqlalchemy import select

from app.config import get_settings
from app.database.session import async_session_factory
from app.models.calendar import Calendar
from app.models.organization import Organization

logger = logging.getLogger("seed_pilot_roster")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ORG_SLUG = "mhc"
TIMEZONE = "America/Edmonton"
ROSTER_KIND = "primaries_roster"

WORK_START = time(9, 0)
WORK_END = time(17, 0)
TITLE_LABEL = "8:45 AM to 5:15 PM"


@dataclass(frozen=True)
class RosterEntry:
    name: str
    territory: str

ROSTER_MANIFEST: List[RosterEntry] = [
    RosterEntry(name="Aaron B.", territory="north"),
    RosterEntry(name="McKenzi S.", territory="south"),
]


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
    if settings.google_calendar_delegation_email:
        creds = creds.with_subject(settings.google_calendar_delegation_email)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _expected_title(name: str) -> str:
    return f"Primaries - {name} - {TITLE_LABEL}"


def _list_events_on(svc, calendar_id: str, target: date_cls) -> List[Dict[str, Any]]:
    tz = ZoneInfo(TIMEZONE)
    start = datetime.combine(target, time(0, 0), tzinfo=tz)
    end = datetime.combine(target, time(23, 59, 59), tzinfo=tz)
    out: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        resp = svc.events().list(
            calendarId=calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            maxResults=250,
            pageToken=page_token,
        ).execute()
        out.extend(resp.get("items", []) or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def _insert_event(svc, calendar_id: str, title: str, target: date_cls) -> str:
    tz = ZoneInfo(TIMEZONE)
    start = datetime.combine(target, WORK_START, tzinfo=tz)
    end = datetime.combine(target, WORK_END, tzinfo=tz)
    body = {
        "summary": title,
        "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
        "description": (
            "Roster event — created by the Sarah backend service account. "
            "Do not edit by hand; manage via the Comms Calendar UI or a "
            "backend operator script."
        ),
    }
    created = svc.events().insert(calendarId=calendar_id, body=body).execute()
    return created["id"]


async def _resolve_roster_calendar() -> tuple[str, str]:
    """Return (organization_id, roster_calendar_google_id) or raise."""
    async with async_session_factory() as db:
        org = (
            await db.execute(select(Organization).where(Organization.slug == ORG_SLUG))
        ).scalar_one_or_none()
        if org is None:
            raise RuntimeError(f"organization '{ORG_SLUG}' not found")
        roster = (
            await db.execute(
                select(Calendar).where(
                    Calendar.organization_id == org.id,
                    Calendar.kind == ROSTER_KIND,
                    Calendar.active.is_(True),
                )
            )
        ).scalars().first()
        if roster is None:
            raise RuntimeError(
                f"no active {ROSTER_KIND} calendar for org '{ORG_SLUG}' — "
                "run scripts.provision_pilot_calendars first"
            )
        return str(org.id), roster.google_id


async def main(target_date: date_cls, dry_run: bool, only: Optional[str]) -> int:
    org_id, roster_gid = await _resolve_roster_calendar()
    logger.info("roster calendar: %s", roster_gid)
    logger.info("target date    : %s (%s)", target_date.isoformat(), TIMEZONE)
    if dry_run:
        logger.info("DRY RUN — no events will be inserted")

    svc = _build_google_service()
    existing = _list_events_on(svc, roster_gid, target_date) if not dry_run else []
    existing_summaries = {(e.get("summary") or "").strip() for e in existing}
    if existing:
        logger.info("found %d existing events on %s", len(existing), target_date)

    rows: List[Dict[str, Any]] = []
    entries = (
        [e for e in ROSTER_MANIFEST if e.name == only]
        if only
        else list(ROSTER_MANIFEST)
    )
    if only and not entries:
        raise RuntimeError(f"--primary '{only}' not found in ROSTER_MANIFEST")

    for entry in entries:
        title = _expected_title(entry.name)
        if title in existing_summaries:
            rows.append({"name": entry.name, "title": title, "action": "SKIPPED", "event_id": "(exists)"})
            continue
        if dry_run:
            rows.append({"name": entry.name, "title": title, "action": "WOULD-CREATE", "event_id": "-"})
            continue
        event_id = _insert_event(svc, roster_gid, title, target_date)
        rows.append({"name": entry.name, "title": title, "action": "CREATED", "event_id": event_id})

    print()
    print(f"{'NAME':<14} {'ACTION':<14} {'EVENT_ID':<40} TITLE")
    print("-" * 110)
    for r in rows:
        print(f"{r['name']:<14} {r['action']:<14} {r['event_id']:<40} {r['title']}")
    print()
    if dry_run:
        print("DRY RUN — re-run without --dry-run to apply.")
    else:
        print("Done. Sarah's _propose_at_need will see these directors as on-shift on %s." % target_date)
        print("Smoke-test by talking to Sarah at park_memorial (expect McKenzi S. @ PM-1)")
        print("and chapel_of_the_bells (expect Aaron B. @ CH-1).")
    return 0


def _parse_date(s: str) -> date_cls:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid date '{s}': expected YYYY-MM-DD") from e


def _default_target_date() -> date_cls:
    """Tomorrow in Edmonton (so the date is sensible regardless of operator's TZ)."""
    now_edm = datetime.now(ZoneInfo(TIMEZONE))
    return (now_edm + timedelta(days=1)).date()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed roster events on the MHC Primaries Roster calendar.")
    parser.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        help="YYYY-MM-DD (Edmonton-local). Defaults to tomorrow Edmonton.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan only; no Google writes.")
    parser.add_argument(
        "--primary",
        type=str,
        default=None,
        help="Only seed for this Primary name (must match ROSTER_MANIFEST exactly).",
    )
    args = parser.parse_args()
    target = args.date or _default_target_date()
    sys.exit(asyncio.run(main(target, args.dry_run, args.primary)))
