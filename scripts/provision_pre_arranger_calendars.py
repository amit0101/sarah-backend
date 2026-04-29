"""Provision the 6 SA-owned pre-arranger calendars under our service account
and link them into `sarah.calendars` on the `mhc` org.

USAGE
  cd backend
  python -m scripts.provision_pre_arranger_calendars              # apply
  python -m scripts.provision_pre_arranger_calendars --dry-run    # report only
  python -m scripts.provision_pre_arranger_calendars --enable-flag  # also flip pre_arrangers_enabled=true

CONTEXT
  Per `mh_venues_brief.html` (lines 718, 778, 797, 857), pre-need bookings
  route to one of 6 named pre-arrangers — Shannon, Bill, Chris, Charles,
  Audrey, Barb — sales-side staff who book flexibly (no fixed slots, no
  venue cap, no territory routing). Their own calendar is the only
  constraint on a pre-need slot.

  We mint these as SA-owned calendars (mirroring the 22 venue calendars
  pattern in `provision_pilot_calendars.py`). SA writes/reads directly.
  No DWD. Optional ACL share to each pre-arranger's @mhfh.com email is a
  post-launch nicety (do not block on it).

  This script is the pre-need analogue of `provision_pilot_calendars.py`.

WHAT IT DOES (idempotent — safe to re-run):
  1. List the calendars currently visible to the service account.
  2. For each entry in `PRE_ARRANGER_MANIFEST`:
       a. Reuse the existing calendar if a calendar with the matching
          summary already exists; otherwise create one
          (`calendars().insert`, timeZone = America/Edmonton).
       b. Upsert the corresponding `sarah.calendars` row keyed on
          (organization_id, name, kind='pre_arranger').
  3. Optionally flip `organizations.config.feature_flags.pre_arrangers_enabled`
     to true (`--enable-flag`).
  4. Print a summary table.

WHAT IT DOES NOT DO:
  - Grant ACL to anyone. Calendars are SA-owned and SA-readable; bookings
    surface in the comms dashboard schedule view. Operators can later
    share them to specific @mhfh.com addresses via the Calendar Management
    page (or directly via Google Workspace) if a pre-arranger wants
    bookings to land in their personal Calendar app.
  - Use DWD impersonation. Pre-arranger calendars are SA-owned outright,
    matching the post-session-13 operating model (deployed Sarah is
    SA-direct, no `GOOGLE_CALENDAR_DELEGATION_EMAIL`).

ROSTER (6 pre-arrangers, per `mh_venues_brief.html` line 778):
    Shannon, Bill, Chris, Charles, Audrey, Barb
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

logger = logging.getLogger("provision_pre_arranger")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ORG_SLUG = "mhc"
TIMEZONE = "America/Edmonton"


def _pre_arranger(name: str) -> Dict[str, Any]:
    return {
        "name": name,
        "kind": "pre_arranger",
        "read_convention": "busy",
        "metadata": {
            "role": "pre_arranger",
        },
    }


# Pre-arranger roster from `mh_venues_brief.html` line 778. Order matches the
# brief; `_propose_pre_need` walks all candidates equally so order only
# affects display in the Calendar Management UI.
PRE_ARRANGER_MANIFEST: List[Dict[str, Any]] = [
    _pre_arranger("Shannon"),
    _pre_arranger("Bill"),
    _pre_arranger("Chris"),
    _pre_arranger("Charles"),
    _pre_arranger("Audrey"),
    _pre_arranger("Barb"),
]


def _build_google_service():
    """Return an authed Google Calendar v3 client, SA-direct (no DWD).

    Pre-arranger calendars must be SA-owned outright so the post-session-13
    SA-direct deployment model can write to them without delegation. We
    explicitly skip `with_subject` even if `GOOGLE_CALENDAR_DELEGATION_EMAIL`
    is set in the local .env.
    """
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
    # Intentionally NOT calling creds.with_subject(...) — see docstring.
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


async def _enable_pre_arranger_flag(db, org: Organization) -> str:
    cfg = dict(org.config or {})
    flags = dict(cfg.get("feature_flags") or {})
    if flags.get("pre_arrangers_enabled") is True:
        return "ALREADY_ON"
    flags["pre_arrangers_enabled"] = True
    cfg["feature_flags"] = flags
    org.config = cfg
    return "FLIPPED"


def _summary_for(org_slug: str, name: str) -> str:
    return f"{org_slug.upper()} - Pre-Arranger - {name}"


async def main(dry_run: bool, enable_flag: bool) -> int:
    svc = _build_google_service()
    logger.info("listing existing SA calendars (SA-direct, no DWD)...")
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
        for entry in PRE_ARRANGER_MANIFEST:
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
                        description=(
                            f"Sarah pre-arranger calendar for {entry['name']} "
                            f"(kind=pre_arranger). Owned by the service account; "
                            f"safe to share read/write to the pre-arranger's @mhfh.com "
                            f"email if they want bookings to land in their personal "
                            f"Google Calendar app."
                        ),
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

        flag_action = "SKIPPED"
        if enable_flag and not dry_run:
            flag_action = await _enable_pre_arranger_flag(db, org)
        elif enable_flag and dry_run:
            cfg = (org.config or {}).get("feature_flags") or {}
            flag_action = (
                "ALREADY_ON" if cfg.get("pre_arrangers_enabled") is True else "WOULD-FLIP"
            )

        if not dry_run:
            await db.commit()

    print()
    print(f"{'NAME':<14} {'KIND':<14} {'GOOGLE':<14} {'DB':<10} GOOGLE_ID")
    print("-" * 100)
    for r in report:
        print(
            f"{r['name']:<14} {r['kind']:<14} {r['google']:<14} {r['db']:<10} {r['google_id']}"
        )
    print()
    if enable_flag:
        print(f"feature_flags.pre_arrangers_enabled: {flag_action}")
    else:
        print("feature_flags.pre_arrangers_enabled NOT touched (rerun with --enable-flag).")
    print()
    if dry_run:
        print("DRY RUN — no Google calendars created, no DB rows changed.")
    else:
        print("Done. Pre-need flow (`_propose_pre_need`) will start using these")
        print("calendars as soon as `pre_arrangers_enabled = true` on `mhc`.")
    return 0


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    enable = "--enable-flag" in sys.argv
    sys.exit(asyncio.run(main(dry, enable)))
