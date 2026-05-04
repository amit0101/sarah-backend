"""Compare raw httpx vs openai library — same model, same payload.

Hypothesis: the openai library (v2.30.0, prod uses similar) adds material
overhead vs raw HTTP. If so, upgrading or switching code paths could help.
"""
from __future__ import annotations

import asyncio, statistics, time
from typing import Any, Dict, List

import httpx
from dotenv import load_dotenv

load_dotenv()

from openai import AsyncOpenAI
from app.config import get_settings
from app.conversation_engine.prompt_manager import build_system_prompt
from app.conversation_engine.tool_definitions import sarah_tools
from app.database.session import async_session_factory
from app.models.location import Location
from app.models.organization import Organization
from sqlalchemy import select

MODEL = "gpt-5.4-mini"
USER_MSG = "Hi, can you tell me a bit about what McInnis & Holloway offers?"
RUNS = 3


async def get_payload() -> tuple[str, list[dict]]:
    async with async_session_factory() as db:
        org = (await db.execute(select(Organization).where(Organization.slug == "mhc"))).scalar_one()
        loc = (await db.execute(
            select(Location).where(Location.organization_id == org.id, Location.id == "park_memorial")
        )).scalar_one()
        prompt = await build_system_prompt(db, location=loc, path="general")
        all_tools = sarah_tools(vector_store_id=org.vector_store_id)
        tools = [t for t in all_tools if t.get("type") != "file_search"]
        return prompt, tools


async def call_via_httpx(client: httpx.AsyncClient, key: str, prompt: str, tools: list[dict]) -> int:
    body = {
        "model": MODEL, "instructions": prompt, "input": USER_MSG,
        "tools": tools, "max_output_tokens": 400,
        "reasoning": {"effort": "none"},
    }
    t0 = time.monotonic()
    r = await client.post(
        "https://api.openai.com/v1/responses", json=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=240.0,
    )
    dt = int((time.monotonic() - t0) * 1000)
    r.raise_for_status()
    return dt


async def call_via_sdk(sdk: AsyncOpenAI, prompt: str, tools: list[dict]) -> int:
    t0 = time.monotonic()
    await sdk.responses.create(
        model=MODEL, instructions=prompt, input=USER_MSG,
        tools=tools, max_output_tokens=400,
        reasoning={"effort": "none"},
    )
    return int((time.monotonic() - t0) * 1000)


async def main() -> None:
    settings = get_settings()
    key = settings.openai_api_key
    prompt, tools = await get_payload()
    print(f"prompt chars: {len(prompt)}    tools: {len(tools)}    runs: {RUNS}\n")

    httpx_durs: List[int] = []
    sdk_durs: List[int] = []
    sdk_durs_reused: List[int] = []

    async with httpx.AsyncClient() as c:
        sdk = AsyncOpenAI(api_key=key)
        for i in range(RUNS):
            print(f"── run {i + 1}/{RUNS} ──")
            a = await call_via_httpx(c, key, prompt, tools); httpx_durs.append(a)
            print(f"  raw httpx                 {a:>6}ms")
            b = await call_via_sdk(sdk, prompt, tools); sdk_durs.append(b)
            print(f"  openai SDK (reused client) {b:>6}ms")
            # Fresh SDK client per call (mimics any per-call instantiation pattern)
            sdk_fresh = AsyncOpenAI(api_key=key)
            try:
                c2 = await call_via_sdk(sdk_fresh, prompt, tools); sdk_durs_reused.append(c2)
                print(f"  openai SDK (fresh client) {c2:>6}ms")
            finally:
                await sdk_fresh.close()
        await sdk.close()

    def s(xs):
        if not xs: return "no data"
        return f"avg={int(statistics.mean(xs))}ms  med={int(statistics.median(xs))}ms  min={min(xs)}  max={max(xs)}"
    print()
    print("══ summary (model={}, no file_search, effort=none) ══".format(MODEL))
    print(f"  raw httpx                 {s(httpx_durs)}")
    print(f"  openai SDK (reused)       {s(sdk_durs)}")
    print(f"  openai SDK (fresh client) {s(sdk_durs_reused)}")
    if httpx_durs and sdk_durs:
        delta = int(statistics.mean(sdk_durs) - statistics.mean(httpx_durs))
        print(f"\n  SDK overhead vs raw httpx: {delta:+d}ms avg")


if __name__ == "__main__":
    asyncio.run(main())
