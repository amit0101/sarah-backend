"""Grant the delegation identity (mhgdocsmaster@mhfh.com) writer access to
the 4 SA-owned pilot venue calendars.

Why this is needed
------------------
The deployed Sarah backend on Render runs the Google Calendar API as
`mhgdocsmaster@mhfh.com` via domain-wide delegation (so it inherits M&H's
pre-existing access to j.hagel@mhfh.com and the shared Primaries roster).
The 4 pilot venue calendars (PM-1, PM-2, CH-1, CH-2) we created in
session 11 are owned by the service account, which the delegated identity
has no relationship to. Without an explicit ACL grant the deployed Sarah
can't read or write venue events and falls back to the legacy path.

Idempotent: re-running this only writes ACL rules that are missing or have
the wrong role.

USAGE
  cd backend
  python -m scripts.grant_pilot_venue_acls --dry-run
  python -m scripts.grant_pilot_venue_acls
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Dict, List

from google.oauth2 import service_account
from googleapiclient.discovery import build
from sqlalchemy import select

from app.config import get_settings
from app.database.session import async_session_factory
from app.models.calendar import Calendar
from app.models.organization import Organization

ORG_SLUG = "mhc"
DELEGATION_EMAIL = "mhgdocsmaster@mhfh.com"
TARGET_ROLE = "writer"  # 'reader' | 'writer' | 'owner'


def _build_google_service():
    settings = get_settings()
    raw = (settings.google_calendar_credentials or "").strip()
    if not raw:
        raise RuntimeError("GOOGLE_CALENDAR_CREDENTIALS not set")
    scopes = [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.acls",
    ]
    if raw.startswith("{"):
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = service_account.Credentials.from_service_account_file(raw, scopes=scopes)
    # ACL writes happen as the SA owner — no delegation here.
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _existing_user_rule(svc, calendar_id: str, email: str) -> Dict[str, Any] | None:
    rules = svc.acl().list(calendarId=calendar_id).execute().get("items", [])
    for rule in rules:
        scope = rule.get("scope") or {}
        if scope.get("type") == "user" and (scope.get("value") or "").lower() == email.lower():
            return rule
    return None


def _grant(svc, calendar_id: str, email: str, role: str) -> str:
    body = {
        "role": role,
        "scope": {"type": "user", "value": email},
    }
    res = svc.acl().insert(calendarId=calendar_id, body=body).execute()
    return res.get("id", "?")


def _patch(svc, calendar_id: str, rule_id: str, role: str) -> str:
    res = svc.acl().patch(calendarId=calendar_id, ruleId=rule_id, body={"role": role}).execute()
    return res.get("id", "?")


async def _list_pilot_venue_calendars() -> List[Calendar]:
    async with async_session_factory() as db:
        org = (
            await db.execute(select(Organization).where(Organization.slug == ORG_SLUG))
        ).scalar_one_or_none()
        if org is None:
            raise RuntimeError(f"organization slug={ORG_SLUG} not found")
        rows = (
            await db.execute(
                select(Calendar).where(
                    Calendar.organization_id == org.id,
                    Calendar.kind == "venue",
                    Calendar.active.is_(True),
                ).order_by(Calendar.name)
            )
        ).scalars().all()
        return list(rows)


async def main(dry: bool) -> int:
    print(f"DRY RUN: {dry}")
    print(f"Target email: {DELEGATION_EMAIL}  (role={TARGET_ROLE})")
    venues = await _list_pilot_venue_calendars()
    print(f"\nVenue calendars to update: {len(venues)}")
    for v in venues:
        print(f"  {v.name:8s}  {v.google_id}")

    svc = _build_google_service()
    for v in venues:
        print(f"\n── {v.name}")
        try:
            existing = _existing_user_rule(svc, v.google_id, DELEGATION_EMAIL)
        except Exception as e:
            print(f"  ERROR listing ACL: {type(e).__name__}: {e}")
            continue

        if existing is None:
            if dry:
                print(f"  DRY: would INSERT user={DELEGATION_EMAIL} role={TARGET_ROLE}")
            else:
                rule_id = _grant(svc, v.google_id, DELEGATION_EMAIL, TARGET_ROLE)
                print(f"  ✓ inserted rule id={rule_id} role={TARGET_ROLE}")
        elif existing.get("role") != TARGET_ROLE:
            if dry:
                print(
                    f"  DRY: would PATCH rule id={existing['id']} "
                    f"{existing.get('role')!r} → {TARGET_ROLE!r}"
                )
            else:
                rule_id = _patch(svc, v.google_id, existing["id"], TARGET_ROLE)
                print(f"  ✓ patched rule id={rule_id} → role={TARGET_ROLE}")
        else:
            print(f"  ✓ already grants {DELEGATION_EMAIL} role={TARGET_ROLE}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.dry_run)))
