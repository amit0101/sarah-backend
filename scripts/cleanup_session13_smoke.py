"""Clean up session-13 smoke artifacts (Dev Smoketest A/B + their events).

Targets:
  1. Google events on Primaries + venue calendars created by smoke runs (find by sarah.appointments.contact_id matching Smoketest contacts)
  2. sarah.appointments + cascade
  3. sarah.contacts named 'Dev Smoketest A/B' + cascade (conversations, messages, openai_response_logs)
  4. comms.contacts + cascade
  5. GHL contacts (looked up by name via search)

USAGE
  python -m scripts.cleanup_session13_smoke --dry-run
  python -m scripts.cleanup_session13_smoke
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import List

import httpx
from google.oauth2 import service_account
from googleapiclient.discovery import build
from sqlalchemy import text

from app.config import get_settings
from app.database.session import async_session_factory

SMOKETEST_NAMES = ["Dev Smoketest A", "Dev Smoketest B"]
GHL_LOCATION_ID = "S703WHSXhCWXaI0K86Cz"
GHL_API_KEY = os.environ.get("GHL_API_KEY") or "pit-3171ad75-8693-4c51-a61f-578d1ebeea55"
GHL_API_BASE = "https://services.leadconnectorhq.com"
GHL_VERSION = "2021-07-28"


def _build_google_service():
    settings = get_settings()
    raw = (settings.google_calendar_credentials or "").strip()
    if not raw:
        raise RuntimeError("GOOGLE_CALENDAR_CREDENTIALS not set")
    scopes = ["https://www.googleapis.com/auth/calendar"]
    if raw.startswith("{"):
        creds = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
    else:
        creds = service_account.Credentials.from_service_account_file(raw, scopes=scopes)
    # Deliberately no with_subject — SA-direct mode (matches deployed Sarah).
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


async def main(dry: bool) -> int:
    print(f"DRY RUN: {dry}\n")

    # === Phase 1: collect Google event ids from sarah.appointments before we delete the rows ===
    google_event_targets: List[tuple[str, str]] = []  # (calendar_google_id, event_id)
    async with async_session_factory() as db:
        rows = (await db.execute(text(
            """
            SELECT a.id, a.google_event_id, a.google_venue_event_id,
                   pc.google_id AS primary_cal_gid,
                   vc.google_id AS venue_cal_gid,
                   c.name AS contact_name
            FROM sarah.appointments a
            JOIN sarah.contacts c ON c.id = a.contact_id
            LEFT JOIN sarah.calendars pc ON pc.id = a.primary_cal_id
            LEFT JOIN sarah.calendars vc ON vc.id = a.venue_cal_id
            WHERE c.name = ANY(:names)
            """
        ), {"names": SMOKETEST_NAMES})).all()

        appt_ids: List[str] = []
        print(f"── Smoketest sarah.appointments found: {len(rows)}")
        for r in rows:
            appt_ids.append(str(r.id))
            print(f"   appt id={r.id}  contact={r.contact_name}")
            if r.primary_cal_gid and r.google_event_id:
                google_event_targets.append((r.primary_cal_gid, r.google_event_id))
                print(f"      primary  cal={r.primary_cal_gid[:30]}…  event={r.google_event_id}")
            if r.venue_cal_gid and r.google_venue_event_id:
                google_event_targets.append((r.venue_cal_gid, r.google_venue_event_id))
                print(f"      venue    cal={r.venue_cal_gid[:30]}…  event={r.google_venue_event_id}")

    # === Phase 2: delete the Google events ===
    # Also scan Primaries + venue calendars defensively for any Smoketest-named events,
    # since sarah.appointments rows may not always have google_event_id populated.
    PRIMARIES = "5c309c2d3cbfaaae6da6619cac2fe8d6e32392c503ab231c3813512ddc54f662@group.calendar.google.com"
    print("\n── Deleting Google events")
    svc = _build_google_service() if not dry else None

    # Scan Primaries for smoketest events
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=4)

    if not dry:
        try:
            events = svc.events().list(
                calendarId=PRIMARIES,
                timeMin=now.isoformat(),
                timeMax=end.isoformat(),
                maxResults=100,
                singleEvents=True,
            ).execute().get("items", [])
            for e in events:
                summary = (e.get("summary") or "").lower()
                if any(k in summary for k in ("smoketest", "a family", "b family")):
                    google_event_targets.append((PRIMARIES, e["id"]))
                    print(f"   scan: found smoketest event on Primaries: {e['id']} ({e.get('summary')})")
        except Exception as e:
            print(f"   warn scanning Primaries: {type(e).__name__}: {e}")

        # Scan venue calendars too (look up active venue calendar google_ids from DB)
        async with async_session_factory() as db2:
            venue_gids = (await db2.execute(text(
                "SELECT google_id FROM sarah.calendars WHERE kind='venue' AND active=true"
            ))).scalars().all()
        for vgid in venue_gids:
            try:
                events = svc.events().list(
                    calendarId=vgid,
                    timeMin=now.isoformat(),
                    timeMax=end.isoformat(),
                    maxResults=100,
                    singleEvents=True,
                ).execute().get("items", [])
                for e in events:
                    summary = (e.get("summary") or "").lower()
                    if any(k in summary for k in ("smoketest", "a family", "b family")):
                        google_event_targets.append((vgid, e["id"]))
                        print(f"   scan: found smoketest event on {vgid[:30]}…: {e['id']} ({e.get('summary')})")
            except Exception as e:
                continue

        # Dedup
        seen = set()
        unique = []
        for cal, eid in google_event_targets:
            key = (cal, eid)
            if key not in seen:
                seen.add(key)
                unique.append(key)
        google_event_targets = unique

        for cal, eid in google_event_targets:
            try:
                svc.events().delete(calendarId=cal, eventId=eid).execute()
                print(f"   ✓ deleted event {eid} on {cal[:30]}…")
            except Exception as e:
                print(f"   warn: {type(e).__name__}: {e} (cal={cal[:30]}… eid={eid})")
    else:
        for cal, eid in google_event_targets:
            print(f"   DRY: would delete event {eid} on {cal[:30]}…")
        print(f"   DRY: would also scan Primaries + venue calendars for any Smoketest summary patterns")

    # === Phase 3: DB cleanup ===
    print("\n── DB cleanup")
    async with async_session_factory() as db:
        # Find sarah contact ids
        sarah_ids = (await db.execute(text(
            "SELECT id FROM sarah.contacts WHERE name = ANY(:names)"
        ), {"names": SMOKETEST_NAMES})).scalars().all()
        print(f"   sarah.contacts: {len(sarah_ids)} target(s)")

        if not dry:
            params = {"ids": list(sarah_ids), "names": SMOKETEST_NAMES}
            await db.execute(text("DELETE FROM sarah.appointments WHERE contact_id = ANY(:ids)"), params)
            await db.execute(text("""
                DELETE FROM sarah.openai_response_logs
                WHERE conversation_id IN (
                    SELECT id FROM sarah.conversations WHERE contact_id = ANY(:ids)
                )
            """), params)
            await db.execute(text("""
                DELETE FROM sarah.messages WHERE conversation_id IN (
                    SELECT id FROM sarah.conversations WHERE contact_id = ANY(:ids)
                )
            """), params)
            await db.execute(text("DELETE FROM sarah.conversations WHERE contact_id = ANY(:ids)"), params)
            await db.execute(text("DELETE FROM sarah.contacts WHERE id = ANY(:ids)"), params)

            # comms
            await db.execute(text("""
                DELETE FROM comms.messages WHERE conversation_id IN (
                    SELECT id FROM comms.conversations WHERE contact_id IN (
                        SELECT id FROM comms.contacts WHERE name = ANY(:names)
                    )
                )
            """), params)
            await db.execute(text("""
                DELETE FROM comms.conversations WHERE contact_id IN (
                    SELECT id FROM comms.contacts WHERE name = ANY(:names)
                )
            """), params)
            await db.execute(text("DELETE FROM comms.contacts WHERE name = ANY(:names)"), params)
            await db.commit()
            print("   ✓ DB cleanup done")
        else:
            print("   DRY: would cascade-delete sarah + comms rows")

    # === Phase 4: GHL cleanup — search by name ===
    print("\n── GHL cleanup")
    headers = {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Version": GHL_VERSION,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        for name in SMOKETEST_NAMES:
            try:
                r = await client.post(
                    f"{GHL_API_BASE}/contacts/search",
                    headers=headers,
                    json={"locationId": GHL_LOCATION_ID, "query": name, "pageLimit": 10},
                )
                if r.status_code != 201 and r.status_code != 200:
                    print(f"   warn: search {name} → {r.status_code} {r.text[:200]}")
                    continue
                data = r.json()
                contacts = data.get("contacts") or []
                for c in contacts:
                    cid = c.get("id")
                    cname = (c.get("contactName") or c.get("firstName") or "") + " " + (c.get("lastName") or "")
                    if name.lower() not in cname.lower():
                        continue
                    if dry:
                        print(f"   DRY: would DELETE GHL contact {cid} ({cname.strip()})")
                    else:
                        d = await client.delete(f"{GHL_API_BASE}/contacts/{cid}", headers=headers)
                        if d.status_code in (200, 204):
                            print(f"   ✓ deleted GHL contact {cid} ({cname.strip()})")
                        else:
                            print(f"   warn: delete {cid} → {d.status_code} {d.text[:200]}")
            except Exception as e:
                print(f"   warn: GHL {name} → {type(e).__name__}: {e}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    # Load .env
    import re
    envp = "/Users/amit/PycharmProjects/sarah-chatbot/backend/.env"
    if os.path.exists(envp):
        with open(envp) as f:
            for line in f:
                m = re.match(r"^([A-Z_]+)=(.*)$", line.rstrip("\n"))
                if m:
                    k, v = m.group(1), m.group(2)
                    if v.startswith('"') and v.endswith('"'):
                        v = v[1:-1]
                    os.environ.setdefault(k, v)

    sys.exit(asyncio.run(main(args.dry_run)))
