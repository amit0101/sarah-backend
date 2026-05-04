"""Compare Chat Completions vs Responses API latency on Sarah's actual workload.

Hypothesis: prod's 8-10s/call is largely the Responses API + tool spec
overhead, not the model itself. The simple bench used Chat Completions
direct, which doesn't include this overhead.

Same system prompt, same user message. Three configurations:
  A) Chat Completions, no tools                      (= bench_simple)
  B) Responses API, no tools, no file_search
  C) Responses API, 12 Sarah tools + file_search     (= prod)
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from typing import List

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

MODEL = "gpt-4.1-mini"
USER_MSG = "Hi, can you tell me a bit about what McInnis & Holloway offers?"
RUNS = 3


async def get_prompt_and_tools() -> tuple[str, list[dict], str]:
    async with async_session_factory() as db:
        org = (await db.execute(select(Organization).where(Organization.slug == "mhc"))).scalar_one()
        loc = (await db.execute(
            select(Location).where(
                Location.organization_id == org.id, Location.id == "park_memorial",
            )
        )).scalar_one()
        prompt = await build_system_prompt(db, location=loc, path="general")
        vs = org.vector_store_id
        tools = sarah_tools(vector_store_id=vs)
        return prompt, tools, vs or ""


async def call_chat(client: httpx.AsyncClient, key: str, prompt: str, msg: str) -> int:
    t0 = time.monotonic()
    r = await client.post(
        "https://api.openai.com/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": msg}],
            "max_tokens": 300,
        },
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=120.0,
    )
    dt = int((time.monotonic() - t0) * 1000)
    r.raise_for_status()
    return dt


async def call_responses(
    client: httpx.AsyncClient,
    key: str,
    prompt: str,
    msg: str,
    tools: list[dict] | None,
) -> int:
    body = {
        "model": MODEL,
        "instructions": prompt,
        "input": msg,
        "max_output_tokens": 300,
    }
    if tools:
        body["tools"] = tools
    t0 = time.monotonic()
    r = await client.post(
        "https://api.openai.com/v1/responses",
        json=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=180.0,
    )
    dt = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        print(f"  ERR {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    return dt


async def main() -> None:
    settings = get_settings()
    key = settings.openai_api_key
    prompt, prod_tools, vs = await get_prompt_and_tools()
    print(f"prompt chars: {len(prompt)}    vector_store_id: {vs or '(none)'}")
    print(f"prod tool count: {len(prod_tools)}")
    print(f"model: {MODEL}    runs: {RUNS}    user_msg: {USER_MSG!r}\n")

    A: List[int] = []
    B: List[int] = []
    C: List[int] = []
    async with httpx.AsyncClient() as c:
        for i in range(RUNS):
            print(f"── run {i + 1}/{RUNS} ──")
            a = await call_chat(c, key, prompt, USER_MSG); A.append(a); print(f"  A) Chat Completions, no tools           {a:>6}ms")
            b = await call_responses(c, key, prompt, USER_MSG, None); B.append(b); print(f"  B) Responses API, no tools              {b:>6}ms")
            cval = await call_responses(c, key, prompt, USER_MSG, prod_tools); C.append(cval); print(f"  C) Responses API, 12 tools + file_search {cval:>6}ms")

    def s(xs):
        return f"avg={int(statistics.mean(xs))}ms  med={int(statistics.median(xs))}ms  min={min(xs)}  max={max(xs)}"
    print()
    print("══ summary ══")
    print(f"  A) Chat Completions, no tools             {s(A)}")
    print(f"  B) Responses API, no tools                {s(B)}")
    print(f"  C) Responses API, 12 tools + file_search  {s(C)}")
    print()
    if A and B:
        print(f"  Responses API overhead vs Chat:         +{int(statistics.mean(B) - statistics.mean(A))}ms avg")
    if B and C:
        print(f"  Tools+file_search overhead on Responses: +{int(statistics.mean(C) - statistics.mean(B))}ms avg")


if __name__ == "__main__":
    asyncio.run(main())
