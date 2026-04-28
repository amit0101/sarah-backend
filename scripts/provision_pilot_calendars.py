"""Pilot: mint 7 Google calendars under our service account and link them
into `sarah.calendars` on the `mhc` org.

USAGE
  cd backend
  python -m scripts.provision_pilot_calendars              # apply
  python -m scripts.provision_pilot_calendars --dry-run    # report only

WHAT IT DOES (in this order, idempotent — safe to re-run):
  1. List the calendars currently visible to the service account.
  2. For each entry in `PILOT_MANIFEST`:
       a. Reuse the existing calendar if a calendar with the matching
          summary already exists; otherwise create one
          (`calendars().insert`, timeZone = America/Edmonton).
       b. Upsert the corresponding `sarah.calendars` row keyed on
          (organization_id, name, kind). Existing placeholder rows from
          `seed_venues_and_pre_arrangers.sql` get their `google_id` updated
          in-place; new rows are inserted.
  3. Print a summary table.

WHAT IT DOES NOT DO (do these manually after the script runs):
  - Flip `feature_flags.room_calendars_enabled` on org `mhc`. Do that from
    the Supabase SQL editor once you've smoke-tested propose_slots.
  - Share the calendars with M&H staff. Add ACLs separately when staff
    need read or write access — the SA owns them and that's enough for
    Sarah + the comms-platform booker.
  - Seed any roster events. Drop one event into `MHC - Primaries Roster`
    yourself (Google Calendar UI, ~30 seconds):
        Title: 'Primaries - Aaron B. - 8:45 AM to 5:15 PM'
        Date:  tomorrow, 09:00–17:00 America/Edmonton
    Then add a second event for `McKenzi S.` for the same day.

PILOT SCOPE — see APPOINTMENTS_ARCHITECTURE.md §9. 7 calendars total:
    1 roster
    2 primaries (one North, one South)
    4 venues across 2 sites (PM-1, PM-2, CH-1, CH-2)

This is the smallest set that exercises the new typed-pool path
end-to-end (`_has_seeded_primaries` flips True on the first primary row).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Dict, List

from google.oauth2 import service_account
from googleapiclient.discovery import build
from sqlalchemy import select

from app.config import get_settings
from app.database.session import async_session_factory
from app.models.calendar import Calendar
from app.models.organization import Organization

logger = logging.getLogger("provision_pilot")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ORG_SLUG = "mhc"
TIMEZONE = "America/Edmonton"

# Pilot scope: minimum set to exercise the new typed-pool path end-to-end.
# Two sites (one North, one South), one director per territory, plus the
# shared roster.
PILOT_MANIFEST: List[Dict[str, Any]] = [
    # ── Roster ────────────────────────────────────────────────────────────
    {
        "name": "Primaries Roster",
        "kind": "primaries_roster",
        "read_convention": "availability",
        "metadata": {
            "summary_pattern": r"Primaries\s*-\s*(.+?)\s*-\s*\d",
        },
    },
    # ── Primaries (one per territory for pilot) ───────────────────────────
    {
        "name": "Aaron B.",
        "kind": "primary",
        "read_convention": "busy",
        "metadata": {"first_name": "Aaron", "last_name": "B.", "territory": "north"},
    },
    {
        "name": "McKenzi S.",
        "kind": "primary",
        "read_convention": "busy",
        "metadata": {"first_name": "McKenzi", "last_name": "S.", "territory": "south"},
    },
    # ── Venues — Park Memorial (South), 2 of 4 ────────────────────────────
    {
        "name": "PM-1",
        "kind": "venue",
        "read_convention": "busy",
        "metadata": {"location_slug": "park_memorial", "territory": "south", "slot_index": 1},
    },
    {
        "name": "PM-2",
        "kind": "venue",
        "read_convention": "busy",
        "metadata": {"location_slug": "park_memorial", "territory": "south", "slot_index": 2},
    },
    # ── Venues — Chapel of the Bells (North), 2 of 3 ──────────────────────
    {
        "name": "CH-1",
        "kind": "venue",
        "read_convention": "busy",
        "metadata": {"location_slug": "chapel_of_the_bells", "territory": "north", "slot_index": 1},
    },
    {
        "name": "CH-2",
        "kind": "venue",
        "read_convention": "busy",
        "metadata": {"location_slug": "chapel_of_the_bells", "territory": "north", "slot_index": 2},
    },
]


def _build_google_service():
    """Return an authed Google Calendar v3 client (sync; we wrap the calls)."""
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


def _list_existing_calendars(svc) -> Dict[str, str]:
    """Return {summary: google_id} of every calendar the SA can see (paged)."""
    out: Dict[str, str] = {}
    page_token = None
    while True:
        resp = svc.calendarList().list(pageToken=page_token, maxResults=250).execute()
        for item in resp.get("items", []):
            summary = item.get("summary") or ""
            cal_id = item.get("id")
            if summary and cal_id and summary not in out:
                out[summary] = cal_id
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def _create_calendar(svc, summary: str, description: str = "") -> str:
    body: Dict[str, Any] = {"summary": summary, "timeZone": TIMEZONE}
    if description:
        body["description"] = description
    created = svc.calendars().insert(body=body).execute()
    return created["id"]


async def _upsert_calendar_row(
    db,
    org_id,
    *,
    name: str,
    kind: str,
    google_id: str,
    read_convention: str,
    metadata: Dict[str, Any],
) -> str:
    """INSERT a new sarah.calendars row, or UPDATE the placeholder if one exists.

    Keyed on (organization_id, name, kind) so a re-run after the placeholder
    seed SQL was applied just refreshes the google_id.
    """
    stmt = select(Calendar).where(
        Calendar.organization_id == org_id,
        Calendar.name == name,
        Calendar.kind == kind,
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = Calendar(
            organization_id=org_id,
            name=name,
            kind=kind,
            google_id=google_id,
            read_convention=read_convention,
            active=True,
            metadata_=metadata,
        )
        db.add(row)
        return "INSERTED"
    changed = False
    if row.google_id != google_id:
        row.google_id = google_id
        changed = True
    if row.read_convention != read_convention:
        row.read_convention = read_convention
        changed = True
    if row.metadata_ != metadata:
        row.metadata_ = metadata
        changed = True
    if not row.active:
        row.active = True
        changed = True
    return "UPDATED" if changed else "UNCHANGED"


def _summary_for(org_slug: str, name: str) -> str:
    """Namespace the calendar summary by org so multi-tenant SAs stay tidy."""
    return f"{org_slug.upper()} - {name}"


async def main(dry_run: bool) -> int:
    svc = _build_google_service()
    logger.info("listing existing SA calendars...")
    existing = _list_existing_calendars(svc)
    logger.info("  %d calendars visible to the service account", len(existing))

    async with async_session_factory() as db:
        org = (
            await db.execute(select(Organization).where(Organization.slug == ORG_SLUG))
        ).scalar_one_or_none()
        if org is None:
            logger.error("Organization '%s' not found", ORG_SLUG)
            return 1

        report: List[Dict[str, str]] = []
        for entry in PILOT_MANIFEST:
            summary = _summary_for(org.slug, entry["name"])
            cal_id = existing.get(summary)
            cal_action = "REUSED"
            if not cal_id:
                if dry_run:
                    cal_id = f"(would-create:{summary})"
                    cal_action = "WOULD-CREATE"
                else:
                    cal_id = _create_calendar(
                        svc,
                        summary,
                        description=f"Sarah pilot calendar (kind={entry['kind']}). "
                        "Owned by the service account; safe to share.",
                    )
                    cal_action = "CREATED"

            db_action = "DRY"
            if not dry_run:
                db_action = await _upsert_calendar_row(
                    db,
                    org.id,
                    name=entry["name"],
                    kind=entry["kind"],
                    google_id=cal_id,
                    read_convention=entry["read_convention"],
                    metadata=entry["metadata"],
                )

            report.append(
                {
                    "name": entry["name"],
                    "kind": entry["kind"],
                    "google_id": cal_id,
                    "google": cal_action,
                    "db": db_action,
                }
            )

        if not dry_run:
            await db.commit()

    print()
    print(f"{'NAME':<22} {'KIND':<18} {'GOOGLE':<14} {'DB':<10} GOOGLE_ID")
    print("-" * 110)
    for r in report:
        print(
            f"{r['name']:<22} {r['kind']:<18} {r['google']:<14} {r['db']:<10} {r['google_id']}"
        )
    print()
    if dry_run:
        print("DRY RUN — no Google calendars created, no DB rows changed.")
    else:
        print("Done. Next steps:")
        print("  1. Add a roster event in 'MHC - Primaries Roster' for tomorrow:")
        print("       'Primaries - Aaron B. - 8:45 AM to 5:15 PM'  09:00–17:00")
        print("       'Primaries - McKenzi S. - 8:45 AM to 5:15 PM'  09:00–17:00")
        print("  2. Flip the flag on org 'mhc':")
        print("       UPDATE sarah.organizations")
        print("       SET config = jsonb_set(coalesce(config,'{}'::jsonb),")
        print("                              '{feature_flags,room_calendars_enabled}','true')")
        print("       WHERE slug = 'mhc';")
        print("  3. Smoke-test by talking to Sarah at PM and CH locations.")
    return 0


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    sys.exit(asyncio.run(main(dry)))
