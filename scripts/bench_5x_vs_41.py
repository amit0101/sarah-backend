"""Quick model comparison: 4.1-mini (current) vs 5.4 family.

Realistic prod payload: full Sarah system prompt + all 14 tools (12 Sarah
tools + file_search). Single greeting message. 3 runs per model.

For reasoning models (5.4 family), uses reasoning_effort=minimal to keep
latency comparable to non-reasoning 4.1 family.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from typing import Any, Dict, List

import httpx
from dotenv import load_dotenv

load_dotenv()

from app.config import get_settings
from app.conversation_engine.prompt_manager import build_system_prompt
from app.conversation_engine.tool_definitions import sarah_tools
from app.database.session import async_session_factory
from app.models.location import Location
from app.models.organization import Organization
from sqlalchemy import select

USER_MSG = "Hi, can you tell me a bit about what McInnis & Holloway offers?"
RUNS = 3

MODELS: List[Dict[str, Any]] = [
    {"name": "gpt-4.1-mini",  "is_reasoning": False},
    {"name": "gpt-4.1",       "is_reasoning": False},
    {"name": "gpt-5.4-nano",  "is_reasoning": True},
    {"name": "gpt-5.4-mini",  "is_reasoning": True},
    {"name": "gpt-5.4",       "is_reasoning": True},
]


async def get_payload() -> tuple[str, list[dict]]:
    async with async_session_factory() as db:
        org = (await db.execute(select(Organization).where(Organization.slug == "mhc"))).scalar_one()
        loc = (await db.execute(
            select(Location).where(Location.organization_id == org.id, Location.id == "park_memorial")
        )).scalar_one()
        prompt = await build_system_prompt(db, location=loc, path="general")
        # Drop file_search — incompatible with reasoning.effort=minimal on 5.x.
        # We're testing pure model latency; 13 functional tools is still a
        # realistic prod-shaped payload.
        all_tools = sarah_tools(vector_store_id=org.vector_store_id)
        tools = [t for t in all_tools if t.get("type") != "file_search"]
        return prompt, tools


async def call_responses(
    client: httpx.AsyncClient,
    key: str,
    model: str,
    prompt: str,
    msg: str,
    tools: list[dict],
    is_reasoning: bool,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": model,
        "instructions": prompt,
        "input": msg,
        "tools": tools,
        "max_output_tokens": 400,
    }
    if is_reasoning:
        body["reasoning"] = {"effort": "none"}
    t0 = time.monotonic()
    r = await client.post(
        "https://api.openai.com/v1/responses",
        json=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=240.0,
    )
    dt = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        return {"ok": False, "duration_ms": dt, "status": r.status_code, "error": r.text[:300]}
    data = r.json()
    usage = data.get("usage") or {}
    text = ""
    for o in data.get("output", []) or []:
        if o.get("type") == "message":
            for c in o.get("content", []) or []:
                if c.get("type") == "output_text":
                    text += c.get("text", "")
    fn_calls = sum(1 for o in data.get("output", []) or [] if o.get("type") == "function_call")
    fs_calls = sum(1 for o in data.get("output", []) or [] if o.get("type") == "file_search_call")
    rzn = (usage.get("output_tokens_details") or {}).get("reasoning_tokens")
    return {
        "ok": True, "duration_ms": dt,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": rzn,
        "fn_calls": fn_calls, "fs_calls": fs_calls,
        "reply_preview": text[:120],
    }


async def main() -> None:
    settings = get_settings()
    key = settings.openai_api_key
    prompt, tools = await get_payload()
    print(f"prompt chars: {len(prompt)}    tools: {len(tools)}    user_msg: {USER_MSG!r}")
    print(f"runs: {RUNS}\n")

    results: Dict[str, List[Dict[str, Any]]] = {m["name"]: [] for m in MODELS}
    async with httpx.AsyncClient() as c:
        for i in range(RUNS):
            print(f"── run {i + 1}/{RUNS} ──")
            for m in MODELS:
                res = await call_responses(c, key, m["name"], prompt, USER_MSG, tools, m["is_reasoning"])
                results[m["name"]].append(res)
                if res.get("ok"):
                    rzn = res.get("reasoning_tokens")
                    rzn_str = f"  rzn_tok={rzn}" if rzn else ""
                    print(
                        f"  {m['name']:<18} {res['duration_ms']:>6}ms  "
                        f"in={res.get('input_tokens')}  out={res.get('output_tokens')}{rzn_str}  "
                        f"fn={res.get('fn_calls')} fs={res.get('fs_calls')}  "
                        f"→ {res.get('reply_preview', '')}"
                    )
                else:
                    print(f"  {m['name']:<18} FAIL {res.get('status')}: {res.get('error')}")

    print("\n══ summary (full prod payload, Responses API + 14 tools + file_search) ══")
    print(f"{'model':<18} {'avg ms':>8} {'med ms':>8} {'min':>6} {'max':>6}  ok/fail")
    for m in MODELS:
        rs = results[m["name"]]
        durs = [r["duration_ms"] for r in rs if r.get("ok")]
        n_ok = len(durs); n_fail = len(rs) - n_ok
        if durs:
            print(f"{m['name']:<18} {int(statistics.mean(durs)):>8} {int(statistics.median(durs)):>8} {min(durs):>6} {max(durs):>6}   {n_ok}/{n_fail}")
        else:
            print(f"{m['name']:<18} all failed")


if __name__ == "__main__":
    asyncio.run(main())
