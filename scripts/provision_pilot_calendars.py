"""Provision the 22 SA-owned venue calendars under our service account
and link them into `sarah.calendars` on the `mhc` org.

USAGE
  cd backend
  python -m scripts.provision_pilot_calendars              # apply
  python -m scripts.provision_pilot_calendars --dry-run    # report only

HISTORY — what this script no longer does, and why:
  Session 11 (2026-04-28): seeded 7 SA-owned calendars: 1 Primaries roster,
  2 per-Primary (Aaron B., McKenzi S.), 4 venues. Pilot scope.

  Session 12 (2026-04-29) realigned to M&H's actual operating model:
    - The Primaries roster is M&H's existing
      `5c309c2d3...@group.calendar.google.com` calendar (events-as-availability).
      We do NOT mint a parallel SA-owned roster anymore.
    - Per-Primary calendars are not used. Director busy state is derived by
      substring-matching the director's name against booking events on the
      shared booking calendar (j.hagel@mhfh.com). No `kind='primary'` rows.
    - The 22 venue calendars (one per bookable space across 10 sites) ARE
      SA-owned new infrastructure. THIS SCRIPT.

WHAT IT DOES (idempotent — safe to re-run):
  1. List the calendars currently visible to the service account.
  2. For each entry in `PILOT_MANIFEST`:
       a. Reuse the existing calendar if a calendar with the matching
          summary already exists; otherwise create one
          (`calendars().insert`, timeZone = America/Edmonton).
       b. Upsert the corresponding `sarah.calendars` row keyed on
          (organization_id, name, kind).
  3. Print a summary table.

WHAT IT DOES NOT DO (handled by sibling scripts):
  - Grant `mhgdocsmaster@mhfh.com` writer ACL on each venue (so the deployed
    Sarah, which uses delegation as mhgdocsmaster, can read/write venue
    holds). That's `scripts/grant_pilot_venue_acls.py`.
  - Touch the `kind='primaries_roster'` row (use `realign_pilot_calendars.py`).
  - Flip feature flags. Already on for `mhc` (`room_calendars_enabled=true`).

VENUE INVENTORY (22 venues across 10 sites, per mh_venues_brief.html):
    park_memorial          PM-1, PM-2, PM-3, PM-4   (south, 4)
    chapel_of_the_bells    CH-1, CH-2, CH-3          (north, 3)
    fish_creek             FC-1, FC-2, FC-3          (south, 3)
    calgary_crematorium    CF-1, CF-2                (north, 2)
    deerfoot_south         DS-1, DS-2                (south, 2)
    eastside               ES-1, ES-2                (south, 2)
    heritage               HF-1, HF-2                (north, 2)
    crowfoot               CR-1, CR-2                (north, 2)
    airdrie                AF-1                       (north, 1)
    cochrane               CC-1                       (north, 1)
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

def _venue(code: str, location_slug: str, territory: str, slot_index: int) -> Dict[str, Any]:
    return {
        "name": code,
        "kind": "venue",
        "read_convention": "busy",
        "metadata": {
            "location_slug": location_slug,
            "territory": territory,
            "slot_index": slot_index,
        },
    }


# Full M&H venue inventory (22 venues across 10 sites). Sorted by site then
# slot_index so the on-disk order matches the brief and the auto-pick rule
# (`_venue_calendars_at_location` orders by name, which gives "lowest-numbered
# free venue first" — PM-1 → PM-2 → ... at each site).
PILOT_MANIFEST: List[Dict[str, Any]] = [
    # ── Park Memorial (south, 4 venues) ───────────────────────────────────
    _venue("PM-1", "park_memorial", "south", 1),
    _venue("PM-2", "park_memorial", "south", 2),
    _venue("PM-3", "park_memorial", "south", 3),
    _venue("PM-4", "park_memorial", "south", 4),
    # ── Chapel of the Bells (north, 3 venues) ─────────────────────────────
    _venue("CH-1", "chapel_of_the_bells", "north", 1),
    _venue("CH-2", "chapel_of_the_bells", "north", 2),
    _venue("CH-3", "chapel_of_the_bells", "north", 3),
    # ── Fish Creek (south, 3 venues) ──────────────────────────────────────
    _venue("FC-1", "fish_creek", "south", 1),
    _venue("FC-2", "fish_creek", "south", 2),
    _venue("FC-3", "fish_creek", "south", 3),
    # ── Calgary Crematorium (north, 2 venues) ─────────────────────────────
    _venue("CF-1", "calgary_crematorium", "north", 1),
    _venue("CF-2", "calgary_crematorium", "north", 2),
    # ── Deerfoot South (south, 2 venues) ──────────────────────────────────
    _venue("DS-1", "deerfoot_south", "south", 1),
    _venue("DS-2", "deerfoot_south", "south", 2),
    # ── Eastside (south, 2 venues) ────────────────────────────────────────
    _venue("ES-1", "eastside", "south", 1),
    _venue("ES-2", "eastside", "south", 2),
    # ── Heritage (north, 2 venues) ────────────────────────────────────────
    _venue("HF-1", "heritage", "north", 1),
    _venue("HF-2", "heritage", "north", 2),
    # ── Crowfoot (north, 2 venues) ────────────────────────────────────────
    _venue("CR-1", "crowfoot", "north", 1),
    _venue("CR-2", "crowfoot", "north", 2),
    # ── Airdrie (north, 1 venue) ──────────────────────────────────────────
    _venue("AF-1", "airdrie", "north", 1),
    # ── Cochrane (north, 1 venue) ─────────────────────────────────────────
    _venue("CC-1", "cochrane", "north", 1),
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
        print("Done. Next step (idempotent): grant mhgdocsmaster@mhfh.com")
        print("writer access on the new venue calendars so the deployed Sarah")
        print("(via delegation) can read/write them:")
        print("    python -m scripts.grant_pilot_venue_acls")
    return 0


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    sys.exit(asyncio.run(main(dry)))
