"""Clean up artifacts left by the session-12 smoke tests.

Targets:
  1. Bogus Google Calendar event on j.hagel@mhfh.com (Sarah booked via legacy)
  2. sarah.appointments row 564a36e5
  3. sarah.contacts + cascade (conversations, messages, openai_response_logs)
     for Smoketest A and B
  4. comms.contacts for Smoketest A and B
  5. GHL contacts (Smoketest A and B) — cascades opportunities + tags

USAGE
  cd backend
  python -m scripts.cleanup_smoke_test --dry-run
  python -m scripts.cleanup_smoke_test
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Dict, List, Optional

import httpx
from google.oauth2 import service_account
from googleapiclient.discovery import build
from sqlalchemy import select, text

from app.config import get_settings
from app.database.session import async_session_factory
from app.models.appointment import Appointment
from app.models.contact import Contact
from app.models.conversation import Conversation

JEFF_CAL = "j.hagel@mhfh.com"
DELEGATION_EMAIL = "mhgdocsmaster@mhfh.com"

BOGUS_GOOGLE_EVENT_ID = "krgejmj9a7f2h27bpupl32bs6k"
BOGUS_APPOINTMENT_ID = "564a36e5-3b32-4e63-9f37-9c3b4edf28ee"

SARAH_TEST_CONTACT_NAMES = ["Dev Smoketest A", "Dev Smoketest B"]
SARAH_TEST_CONV_IDS = [
    "62a917fc-58c8-404d-8b55-99889dd172ad",
    "2bf56286-d40d-41cc-afa0-d996dea0c75f",
]

GHL_TEST_CONTACT_IDS = ["Tq96Nbpl0KB9cWw0ORqF", "ILwjHHIdNjgGM0Fg4KFC"]
GHL_LOCATION_ID = "S703WHSXhCWXaI0K86Cz"
GHL_API_KEY = "pit-3171ad75-8693-4c51-a61f-578d1ebeea55"
GHL_API_BASE = "https://services.leadconnectorhq.com"
GHL_VERSION = "2021-07-28"


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
    creds = creds.with_subject(DELEGATION_EMAIL)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


async def _delete_google_event(dry: bool) -> None:
    print(f"\n── Step 1: delete Google event {BOGUS_GOOGLE_EVENT_ID} from {JEFF_CAL}")
    if dry:
        print("   DRY RUN")
        return
    svc = _build_google_service()
    try:
        svc.events().delete(calendarId=JEFF_CAL, eventId=BOGUS_GOOGLE_EVENT_ID).execute()
        print("   ✓ deleted")
    except Exception as e:
        print(f"   warning: {type(e).__name__}: {e}")


async def _delete_db_rows(dry: bool) -> None:
    print("\n── Step 2-4: delete sarah.appointments, contacts, conversations, messages, openai_response_logs")
    async with async_session_factory() as db:
        # Find sarah contact ids matching Smoketest A/B
        smoketest_sarah = (
            await db.execute(
                select(Contact.id, Contact.name).where(Contact.name.in_(SARAH_TEST_CONTACT_NAMES))
            )
        ).all()
        smoketest_ids = [str(row[0]) for row in smoketest_sarah]
        print(f"   sarah.contacts targeted: {len(smoketest_ids)}")
        for row in smoketest_sarah:
            print(f"      {row[1]}  id={row[0]}")

        # Sarah appointments to nuke
        if dry:
            print(f"   DRY: would delete sarah.appointments {BOGUS_APPOINTMENT_ID}")
        else:
            await db.execute(
                text("DELETE FROM sarah.appointments WHERE id = :id"),
                {"id": BOGUS_APPOINTMENT_ID},
            )

        # Sarah cascade — messages, openai_response_logs, conversations
        if smoketest_ids:
            params = {"ids": list(smoketest_ids)}
            if dry:
                print(f"   DRY: would delete sarah.messages, openai_response_logs, conversations, contacts for {len(smoketest_ids)} contact(s)")
            else:
                await db.execute(text("""
                    DELETE FROM sarah.openai_response_logs
                    WHERE conversation_id IN (
                        SELECT id FROM sarah.conversations WHERE contact_id = ANY(:ids)
                    )
                """), params)
                await db.execute(text("""
                    DELETE FROM sarah.messages
                    WHERE conversation_id IN (
                        SELECT id FROM sarah.conversations WHERE contact_id = ANY(:ids)
                    )
                """), params)
                await db.execute(text("""
                    DELETE FROM sarah.conversations WHERE contact_id = ANY(:ids)
                """), params)
                await db.execute(text("""
                    DELETE FROM sarah.contacts WHERE id = ANY(:ids)
                """), params)

        # comms cascade — find by name
        names_param = {"names": list(SARAH_TEST_CONTACT_NAMES)}
        if dry:
            print("   DRY: would delete comms.contacts (and cascade to comms.conversations + comms.messages) for Smoketest A/B")
        else:
            await db.execute(text("""
                DELETE FROM comms.messages WHERE conversation_id IN (
                    SELECT id FROM comms.conversations WHERE contact_id IN (
                        SELECT id FROM comms.contacts WHERE name = ANY(:names)
                    )
                )
            """), names_param)
            await db.execute(text("""
                DELETE FROM comms.conversations WHERE contact_id IN (
                    SELECT id FROM comms.contacts WHERE name = ANY(:names)
                )
            """), names_param)
            await db.execute(text("""
                DELETE FROM comms.contacts WHERE name = ANY(:names)
            """), names_param)

        if not dry:
            await db.commit()
        print("   ✓ DB cleanup done" if not dry else "   (dry run)")


async def _delete_ghl_contacts(dry: bool) -> None:
    print("\n── Step 5: delete GHL contacts (cascades opps + tags)")
    if dry:
        for cid in GHL_TEST_CONTACT_IDS:
            print(f"   DRY: would DELETE /contacts/{cid}")
        return
    headers = {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Version": GHL_VERSION,
        "Accept": "application/json",
    }
    async with httpx.AsyncClient() as client:
        for cid in GHL_TEST_CONTACT_IDS:
            try:
                r = await client.delete(
                    f"{GHL_API_BASE}/contacts/{cid}",
                    headers=headers,
                    timeout=30.0,
                )
                if r.status_code in (200, 204):
                    print(f"   ✓ deleted GHL contact {cid}")
                else:
                    print(f"   warning: GHL {cid} → {r.status_code} {r.text[:200]}")
            except Exception as e:
                print(f"   warning: GHL {cid} → {type(e).__name__}: {e}")


async def main(dry: bool) -> int:
    print(f"DRY RUN: {dry}")
    await _delete_google_event(dry)
    await _delete_db_rows(dry)
    await _delete_ghl_contacts(dry)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.dry_run)))
