"""Inspect ACLs on the pilot calendars — confirm which principals can read them.

USAGE
  cd backend
  python -m scripts.inspect_pilot_acls
"""

from __future__ import annotations

import asyncio
import json
import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build
from sqlalchemy import select

from app.config import get_settings
from app.database.session import async_session_factory
from app.models.calendar import Calendar
from app.models.organization import Organization

ORG_SLUG = "mhc"


def _build_service():
    settings = get_settings()
    raw = (settings.google_calendar_credentials or "").strip()
    if not raw:
        raise RuntimeError("GOOGLE_CALENDAR_CREDENTIALS not set")
    scopes = ["https://www.googleapis.com/auth/calendar"]
    if raw.startswith("{"):
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        sa_email = info.get("client_email")
    else:
        with open(raw) as f:
            info = json.load(f)
        creds = service_account.Credentials.from_service_account_file(raw, scopes=scopes)
        sa_email = info.get("client_email")
    if settings.google_calendar_delegation_email:
        creds = creds.with_subject(settings.google_calendar_delegation_email)
    return build("calendar", "v3", credentials=creds, cache_discovery=False), sa_email


async def main() -> int:
    svc, sa_email = _build_service()
    print(f"local SA: {sa_email}")
    print()

    async with async_session_factory() as db:
        org = (
            await db.execute(select(Organization).where(Organization.slug == ORG_SLUG))
        ).scalar_one_or_none()
        rows = (
            await db.execute(
                select(Calendar).where(
                    Calendar.organization_id == org.id, Calendar.active.is_(True)
                ).order_by(Calendar.kind, Calendar.name)
            )
        ).scalars().all()

    for cal in rows:
        print(f"── {cal.name}  [{cal.kind}]  google_id={cal.google_id[:24]}...")
        try:
            resp = svc.acl().list(calendarId=cal.google_id).execute()
        except Exception as e:
            print(f"   ERROR: {type(e).__name__}: {e}")
            continue
        for item in resp.get("items", []):
            scope = item.get("scope") or {}
            print(
                f"   role={item.get('role'):<8} "
                f"scope_type={scope.get('type'):<8} "
                f"value={scope.get('value', '')}"
            )
        print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
