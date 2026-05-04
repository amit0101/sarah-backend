"""One-shot verification: did the smoke booking land on Primaries?"""
from __future__ import annotations
import json
import os
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

PRIMARIES = "5c309c2d3cbfaaae6da6619cac2fe8d6e32392c503ab231c3813512ddc54f662@group.calendar.google.com"
JEFF = "j.hagel@mhfh.com"


def _svc(scopes=("https://www.googleapis.com/auth/calendar",)):
    raw = os.environ["GOOGLE_CALENDAR_CREDENTIALS"].strip()
    if raw.startswith("{"):
        creds = service_account.Credentials.from_service_account_info(
            json.loads(raw), scopes=list(scopes)
        )
    else:
        creds = service_account.Credentials.from_service_account_file(raw, scopes=list(scopes))
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def main():
    svc = _svc()
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=2)
    tmin = now.isoformat()
    tmax = end.isoformat()

    print("=== PRIMARIES — non-shift events in next 48h ===")
    events = (
        svc.events()
        .list(
            calendarId=PRIMARIES,
            timeMin=tmin,
            timeMax=tmax,
            maxResults=50,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )
    non_shift = [e for e in events if "Primaries -" not in (e.get("summary") or "")]
    for e in non_shift:
        s = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date")
        print(f"  id={e['id']}  start={s}  summary={e.get('summary')}  creator={(e.get('creator') or {}).get('email')}")
    print(f"  ({len(non_shift)} non-shift event(s) found)")

    print()
    print("=== JEFF — events in next 48h (should be empty post-fix) ===")
    # SA may not have access to Jeff's cal anymore (no DWD locally either if env unset).
    try:
        jevents = (
            svc.events()
            .list(calendarId=JEFF, timeMin=tmin, timeMax=tmax, maxResults=50, singleEvents=True)
            .execute()
            .get("items", [])
        )
        for e in jevents:
            s = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date")
            print(f"  id={e['id']}  start={s}  summary={e.get('summary')}  creator={(e.get('creator') or {}).get('email')}")
        print(f"  ({len(jevents)} event(s) on Jeff's calendar)")
    except Exception as e:
        print(f"  (no SA-direct access to Jeff's calendar: {type(e).__name__}) — expected & desired")


if __name__ == "__main__":
    # Load .env locally
    import re

    p = "/Users/amit/PycharmProjects/sarah-chatbot/backend/.env"
    if os.path.exists(p):
        with open(p) as f:
            for line in f:
                m = re.match(r"^([A-Z_]+)=(.*)$", line.rstrip("\n"))
                if m:
                    k, v = m.group(1), m.group(2)
                    if v.startswith('"') and v.endswith('"'):
                        v = v[1:-1]
                    os.environ.setdefault(k, v)
    main()
