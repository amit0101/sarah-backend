"""One-shot read-only listing of events on the Primaries calendar.

Diagnosing why _propose_at_need returns 0 slots: prints all events
in the next 14 days so we can see if (a) shifts exist for the probed
dates and (b) their summary format matches `parse_counselor_from_event`'s
'Primaries - <Name> - <time>' regex.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

PRIMARIES = (
    "5c309c2d3cbfaaae6da6619cac2fe8d6e32392c503ab231c3813512ddc54f662"
    "@group.calendar.google.com"
)


def main() -> None:
    raw = os.environ["GOOGLE_CALENDAR_CREDENTIALS"].strip()
    if raw.startswith("{"):
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/calendar"]
        )
    else:
        creds = service_account.Credentials.from_service_account_file(
            raw, scopes=["https://www.googleapis.com/auth/calendar"]
        )
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=14)
    events = (
        svc.events()
        .list(
            calendarId=PRIMARIES,
            timeMin=now.isoformat(),
            timeMax=end.isoformat(),
            maxResults=250,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )
    print(f"Total events on Primaries (next 14d): {len(events)}")
    for e in events:
        s = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date")
        print(f"  {s}  | {e.get('summary')!r}")


if __name__ == "__main__":
    main()
