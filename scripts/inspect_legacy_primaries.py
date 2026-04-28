"""Read what's actually on the existing M&H Primaries calendar.

Uses delegated identity (mhgdocsmaster@mhfh.com) — same identity the
deployed Sarah uses on Render — so we see exactly what `_check_calendar`'s
legacy fallback sees.

USAGE
  cd backend
  python -m scripts.inspect_legacy_primaries                    # tomorrow Edmonton
  python -m scripts.inspect_legacy_primaries --date 2026-04-29
  python -m scripts.inspect_legacy_primaries --days 7           # whole week
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date as date_cls, datetime, time, timedelta
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

from app.config import get_settings

LEGACY_PRIMARIES_CAL = (
    "5c309c2d3cbfaaae6da6619cac2fe8d6e32392c503ab231c3813512ddc54f662"
    "@group.calendar.google.com"
)
JEFF_CAL = "j.hagel@mhfh.com"
DELEGATION_EMAIL = "mhgdocsmaster@mhfh.com"
TIMEZONE = "America/Edmonton"


def _build_service():
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
    creds = creds.with_subject(DELEGATION_EMAIL)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _list_events(svc, calendar_id: str, day_start: datetime, day_end: datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    page_token = None
    while True:
        resp = svc.events().list(
            calendarId=calendar_id,
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
            pageToken=page_token,
        ).execute()
        out.extend(resp.get("items", []) or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def _fmt_event(ev: Dict[str, Any]) -> str:
    summary = (ev.get("summary") or "(no title)").strip()
    start = (ev.get("start") or {})
    end = (ev.get("end") or {})
    s = start.get("dateTime") or start.get("date") or "?"
    e = end.get("dateTime") or end.get("date") or "?"
    if "T" in s:
        try:
            sdt = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(ZoneInfo(TIMEZONE))
            edt = datetime.fromisoformat(e.replace("Z", "+00:00")).astimezone(ZoneInfo(TIMEZONE))
            time_str = f"{sdt.strftime('%H:%M')}–{edt.strftime('%H:%M')}"
        except Exception:
            time_str = f"{s} → {e}"
    else:
        time_str = f"all-day {s}"
    loc = ev.get("location") or ""
    extras = []
    if loc:
        extras.append(f"loc='{loc}'")
    creator = (ev.get("creator") or {}).get("email")
    if creator:
        extras.append(f"creator={creator}")
    extra_str = ("  " + " ".join(extras)) if extras else ""
    return f"  {time_str:>14}  {summary}{extra_str}"


async def main(start_date: date_cls, days: int) -> int:
    svc = _build_service()
    tz = ZoneInfo(TIMEZONE)
    print(f"Reading as: {DELEGATION_EMAIL}")
    print(f"Calendar  : {LEGACY_PRIMARIES_CAL}")
    print()

    for offset in range(days):
        d = start_date + timedelta(days=offset)
        ds = datetime.combine(d, time(0, 0), tzinfo=tz)
        de = datetime.combine(d, time(23, 59, 59), tzinfo=tz)
        print(f"── {d.strftime('%a %b %d, %Y')} (Edmonton)")
        try:
            events = _list_events(svc, LEGACY_PRIMARIES_CAL, ds, de)
        except Exception as e:
            print(f"   ERROR: {type(e).__name__}: {e}")
            continue
        if not events:
            print("   (no events)")
            continue
        for ev in events:
            print(_fmt_event(ev))
        print()

    print()
    print(f"── {JEFF_CAL} on {start_date.strftime('%a %b %d')}")
    ds = datetime.combine(start_date, time(0, 0), tzinfo=tz)
    de = datetime.combine(start_date, time(23, 59, 59), tzinfo=tz)
    try:
        events = _list_events(svc, JEFF_CAL, ds, de)
    except Exception as e:
        print(f"   ERROR: {type(e).__name__}: {e}")
    else:
        if not events:
            print("   (no events)")
        for ev in events[:25]:
            print(_fmt_event(ev))
        if len(events) > 25:
            print(f"   ... and {len(events) - 25} more")

    return 0


def _parse_date(s: str) -> date_cls:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _default_date() -> date_cls:
    return (datetime.now(ZoneInfo(TIMEZONE)) + timedelta(days=1)).date()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", type=_parse_date, default=None)
    p.add_argument("--days", type=int, default=1)
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.date or _default_date(), max(1, args.days))))
