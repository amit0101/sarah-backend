#!/usr/bin/env python3
"""Read-only smoke probe for the Calendar Management internal API.

Hits the new sarah-backend `/api/internal/calendars*` endpoints with the
shared X-Webhook-Secret. ALL probes here are read-only — listing the
catalog, listing ACLs, listing events. No mutations.

Why this exists
---------------
A2 (session 14) shipped a CRUD surface for the Calendar Mgmt page. Before
flipping any operator-facing modal that POST/PATCHes against prod, we
verify the read surface lights up cleanly against the deployed Render
instance.

Usage
-----
    SARAH_API_BASE=https://sarah-backend-lqoy.onrender.com \\
    SARAH_WEBHOOK_SECRET=xxxxxxxxx \\
    python3 scripts/probe_calendar_management.py mhc

The org slug defaults to `mhc` (the only deployed org today).

Runs against:
    GET /api/internal/calendars?organization_slug=...
    GET /api/internal/org/feature-flags?organization_slug=...
    GET /api/internal/calendars/{id}/acl     (first venue calendar found)
    GET /api/internal/calendars/{id}/events  (first venue calendar found)

Exits non-zero on any failure.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import httpx


def _require_env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        print(f"[fatal] env var {name} is not set", file=sys.stderr)
        sys.exit(2)
    return val


def main() -> int:
    base = os.environ.get("SARAH_API_BASE", "https://sarah-backend-lqoy.onrender.com").rstrip("/")
    secret = _require_env("SARAH_WEBHOOK_SECRET")
    org_slug = sys.argv[1] if len(sys.argv) > 1 else "mhc"

    headers = {"X-Webhook-Secret": secret}

    with httpx.Client(timeout=30.0) as client:
        # 1. List the catalog
        url = f"{base}/api/internal/calendars"
        print(f"[probe] GET {url}?organization_slug={org_slug}")
        r = client.get(url, params={"organization_slug": org_slug}, headers=headers)
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        cals = data.get("calendars") or []
        kinds: dict[str, int] = {}
        for c in cals:
            kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
        print(f"[ok] {len(cals)} rows | by kind: {kinds}")

        # 2. Feature flags
        url = f"{base}/api/internal/org/feature-flags"
        print(f"[probe] GET {url}?organization_slug={org_slug}")
        r = client.get(url, params={"organization_slug": org_slug}, headers=headers)
        r.raise_for_status()
        flags = r.json().get("feature_flags") or {}
        print(f"[ok] flags = {flags}")

        # 3. Pick a venue (most likely to have non-trivial state)
        venue = next((c for c in cals if c["kind"] == "venue"), None)
        if venue is None:
            print("[skip] no venue calendars registered — skipping ACL + events probes")
            return 0
        cal_id = venue["id"]
        cal_name = venue["name"]

        url = f"{base}/api/internal/calendars/{cal_id}/acl"
        print(f"[probe] GET {url} (calendar={cal_name})")
        r = client.get(url, params={"organization_slug": org_slug}, headers=headers)
        r.raise_for_status()
        rules = r.json().get("rules") or []
        print(f"[ok] acl = {len(rules)} rule(s)")
        for rule in rules:
            scope = rule.get("scope") or {}
            print(f"      • {scope.get('type')}:{scope.get('value')} → {rule.get('role')}")

        url = f"{base}/api/internal/calendars/{cal_id}/events"
        print(f"[probe] GET {url} (today)")
        r = client.get(url, params={"organization_slug": org_slug}, headers=headers)
        r.raise_for_status()
        events = r.json().get("events") or []
        print(f"[ok] today's events = {len(events)}")
        for ev in events[:5]:
            start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
            print(f"      • {start}  {ev.get('summary')}")

    print("\n[done] all read-side probes green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
