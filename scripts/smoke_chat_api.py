"""Pilot smoke test: drive Sarah end-to-end via the REST chat API.

USAGE
  cd backend
  python -m scripts.smoke_chat_api                                          # both locations
  python -m scripts.smoke_chat_api --location park_memorial
  python -m scripts.smoke_chat_api --base https://sarah-backend-lqoy.onrender.com

Multi-turn POST /api/chat/message with `organization_slug='mhc'` and a
fixed `location_id`, until Sarah replies with calendar slots. Then:

  1. Asserts Sarah called `check_calendar` (via sarah.openai_response_logs).
  2. Asserts her natural-language reply mentions the expected primary name
     and at least 2 of the 3 expected slot times.

Algorithm correctness is verified separately by `smoke_propose_slots.py`;
this script verifies the LLM → tool → response surface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import httpx
from sqlalchemy import select

from app.database.session import async_session_factory
from app.models.openai_response_log import OpenAIResponseLog

DEFAULT_BASE = os.environ.get("SARAH_BASE_URL", "https://sarah-backend-lqoy.onrender.com")
ORG_SLUG = "mhc"

EXPECTED: Dict[str, Dict[str, Any]] = {
    "park_memorial": {
        # On a real M&H roster Stephanie Hu./McKenzi/Sharon/Stephanie Ho. are
        # all valid south-tagged 9am picks. We assert "any south-tagged
        # director surfaced" by listing the full pool here.
        "primary_pool": [
            "Stephanie Hu.", "McKenzi S.", "Sharon K.", "Jillian G.",
        ],
        "slot_times": ["9:00", "12:15", "3:00"],
        "location_alt": ["Park Memorial"],
    },
    "chapel_of_the_bells": {
        "primary_pool": [
            "Aaron B.", "Ashley R.", "Stephanie Ho.", "Terra S.",
        ],
        "slot_times": ["9:00", "12:15", "3:00"],
        "location_alt": ["Chapel of the Bells", "Chapel"],
    },
}

SCRIPTS: Dict[str, List[str]] = {
    "park_memorial": [
        (
            "Hi, my mother passed away last night and I need to arrange her funeral. "
            "We'd like to come to Park Memorial. My name is Dev Smoketest A, my phone "
            "is 403-555-0101 and my email is dev.smoketest.a@example.com. "
            "Can we come in tomorrow morning?"
        ),
        "Yes, tomorrow works. What times do you have?",
        "Thanks — I'll think it over and call back.",
    ],
    "chapel_of_the_bells": [
        (
            "Hi, my father passed yesterday. We've used Chapel of the Bells before and "
            "want to come there. I'm Dev Smoketest B, phone 403-555-0102, email "
            "dev.smoketest.b@example.com. What time can we come in tomorrow?"
        ),
        "Yes please share the available times for tomorrow.",
        "Thanks — I'll get back to you.",
    ],
}


async def _post_message(
    client: httpx.AsyncClient,
    base: str,
    *,
    location_id: str,
    message: str,
    conversation_id: Optional[str],
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "organization_slug": ORG_SLUG,
        "location_id": location_id,
        "message": message,
    }
    if conversation_id:
        body["conversation_id"] = conversation_id
    r = await client.post(f"{base}/api/chat/message", json=body, timeout=180.0)
    r.raise_for_status()
    return r.json()


async def _fetch_function_calls(conversation_id: UUID) -> List[Dict[str, Any]]:
    """Pull every `function_call` item across all logged OpenAI rounds for this conv.

    Returns list of {"name": str, "arguments": dict} sorted oldest-first.
    """
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(OpenAIResponseLog)
                .where(OpenAIResponseLog.conversation_id == conversation_id)
                .order_by(OpenAIResponseLog.created_at)
            )
        ).scalars().all()
    out: List[Dict[str, Any]] = []
    for row in rows:
        payload = row.payload or {}
        for item in (payload.get("output") or []):
            if (item or {}).get("type") != "function_call":
                continue
            args_raw = item.get("arguments") or "{}"
            try:
                parsed_args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except (ValueError, TypeError):
                parsed_args = {"_raw": args_raw}
            out.append({"name": item.get("name"), "arguments": parsed_args})
    return out


def _verify_reply_text(combined_text: str, expected: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    haystack = combined_text.lower()
    pool = expected.get("primary_pool") or []

    def _name_variants(name: str) -> List[str]:
        """Director names like 'Stephanie Hu.' carry a literal trailing period
        that the LLM frequently drops in natural language ('Stephanie Hu').
        Accept either form."""
        n = name.lower().strip()
        out = [n]
        if n.endswith("."):
            out.append(n.rstrip(".").rstrip())
        return out

    matched = [
        name for name in pool
        if any(v in haystack for v in _name_variants(name))
    ]
    if not matched:
        errors.append(
            f"none of {pool} appears in Sarah's reply (expected one director "
            "name to be surfaced as the primary for these slots)"
        )
    slot_hits = sum(1 for t in expected["slot_times"] if t in combined_text)
    if slot_hits < 2:
        errors.append(
            f"only {slot_hits}/3 of the expected slot times "
            f"{expected['slot_times']} appear in Sarah's reply"
        )
    location_hits = any(tok.lower() in haystack for tok in expected.get("location_alt") or [])
    if not location_hits:
        errors.append(
            f"location name {expected.get('location_alt')} not mentioned in reply (soft signal)"
        )
    return errors


async def run_one(base: str, location: str) -> bool:
    print(f"\n══ {location} ══════════════════════════════════════════════════════════")
    print(f"   POST {base}/api/chat/message  (org={ORG_SLUG}, loc={location})")
    expected = EXPECTED[location]
    script = SCRIPTS[location]

    conv_id: Optional[str] = None
    replies: List[str] = []
    async with httpx.AsyncClient() as client:
        for i, user_text in enumerate(script, 1):
            print(f"\n   [user turn {i}] {user_text}")
            t0 = time.monotonic()
            try:
                resp = await _post_message(
                    client, base,
                    location_id=location,
                    message=user_text,
                    conversation_id=conv_id,
                )
            except httpx.HTTPStatusError as e:
                print(f"   ERROR {e.response.status_code}: {e.response.text[:300]}")
                return False
            dt = time.monotonic() - t0
            conv_id = resp["conversation_id"]
            reply = resp.get("reply", "") or ""
            replies.append(reply)
            preview = (reply.replace("\n", " ")[:380] + "...") if len(reply) > 380 else reply.replace("\n", " ")
            print(f"   [sarah ({dt:.1f}s)] {preview}")

            calls = await _fetch_function_calls(UUID(conv_id))
            check_cal_calls = [c for c in calls if c["name"] == "check_calendar"]
            if check_cal_calls:
                print(f"   → check_calendar called {len(check_cal_calls)}x; "
                      f"args of last: {check_cal_calls[-1]['arguments']}")
                # If Sarah already rendered slot info in this reply, we can stop early.
                if any(t in reply for t in expected["slot_times"]):
                    break

    if not conv_id:
        print("   ✗ FAIL — no conversation_id returned")
        return False

    print(f"\n   conversation_id: {conv_id}")
    calls = await _fetch_function_calls(UUID(conv_id))
    print(f"   tool calls observed across the conversation:")
    for c in calls:
        keys = list(c["arguments"].keys()) if isinstance(c["arguments"], dict) else "?"
        print(f"     · {c['name']:<22} args_keys={keys}")

    check_cal_calls = [c for c in calls if c["name"] == "check_calendar"]
    if not check_cal_calls:
        print("   ✗ FAIL — Sarah never called check_calendar")
        return False

    combined = "\n\n".join(replies)
    errors = _verify_reply_text(combined, expected)
    if errors:
        print("   ✗ FAIL — reply text does not match expectations:")
        for e in errors:
            print(f"     - {e}")
        return False
    haystack = combined.lower()
    surfaced = [
        n for n in (expected.get("primary_pool") or [])
        if n.lower() in haystack or n.lower().rstrip(".").rstrip() in haystack
    ]
    print(f"   ✓ PASS — check_calendar was called; surfaced director(s)={surfaced} "
          f"and slot times for {location}")
    return True


async def main(base: str, locations: List[str]) -> int:
    print(f"sarah_base_url: {base}")
    results: Dict[str, bool] = {}
    for loc in locations:
        results[loc] = await run_one(base, loc)
    print("\n══ SUMMARY ════════════════════════════════════════════════════════════")
    for loc, ok in results.items():
        print(f"  {loc:<25} {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Smoke-test Sarah chat API end-to-end.")
    p.add_argument("--base", default=DEFAULT_BASE, help="Sarah backend base URL.")
    p.add_argument(
        "--location",
        action="append",
        default=None,
        help="Repeatable. Default: both pilot locations.",
    )
    args = p.parse_args()
    locs = args.location or list(EXPECTED.keys())
    sys.exit(asyncio.run(main(args.base, locs)))
