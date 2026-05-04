"""Probe: confirm SA-direct (no DWD) read access on the M&H Primaries calendar.

Usage (from backend/):
    python -m scripts.probe_sa_direct_primaries
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

PRIMARIES_CAL = "5c309c2d3cbfaaae6da6619cac2fe8d6e32392c503ab231c3813512ddc54f662@group.calendar.google.com"


def main() -> int:
    raw = os.environ.get("GOOGLE_CALENDAR_CREDENTIALS")
    if not raw:
        print("ERROR: GOOGLE_CALENDAR_CREDENTIALS not set in env")
        return 2

    scopes = ["https://www.googleapis.com/auth/calendar"]
    raw = raw.strip()
    if raw.startswith("{"):
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = service_account.Credentials.from_service_account_file(raw, scopes=scopes)

    # Deliberately do NOT call creds.with_subject(). SA-direct mode.
    print(f"acting as: {creds.service_account_email}")
    print(f"calendar:  {PRIMARIES_CAL}")
    print()

    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=2)
    try:
        result = (
            svc.events()
            .list(
                calendarId=PRIMARIES_CAL,
                timeMin=now.isoformat(),
                timeMax=end.isoformat(),
                maxResults=20,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except Exception as e:
        print(f"FAIL ({type(e).__name__}): {e}")
        return 1

    items = result.get("items", [])
    print(f"OK — listed {len(items)} event(s) in next 48h:")
    for ev in items[:20]:
        start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
        print(f"  {start}  {ev.get('summary','(no title)')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
