"""Sarah latency bench — simple version (session 20).

Standalone: loads the real merged Sarah system prompt from the DB for
each (path, location) we want to test, then sends the same prompt + a
representative user message to multiple OpenAI + Anthropic models in
turn, measures wall-clock time per call, averages, prints a table.

No tools, no streaming, no Sarah engine. Just system prompt + user msg
→ first response. Apples-to-apples model latency on a realistic prompt.

USAGE
  cd backend
  ANTHROPIC_API_KEY=sk-ant-... .venv/bin/python -m scripts.bench_simple

  # Optional flags
  --location park_memorial      # location slug (org=mhc)
  --runs 3                      # repeats per (model, scenario) for averaging
  --max-tokens 400              # cap output to keep cost down
  --models gpt-4o,gpt-4o-mini,claude-sonnet-4-5,claude-haiku-4-5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.conversation_engine.prompt_manager import build_system_prompt
from app.database.session import async_session_factory
from app.models.location import Location
from app.models.organization import Organization

# Default model set — adjust via --models.
DEFAULT_MODELS = [
    "openai:gpt-4o",
    "openai:gpt-4o-mini",
    "openai:gpt-4.1",
    "openai:gpt-4.1-mini",
    "openai:gpt-4.1-nano",
    "anthropic:claude-sonnet-4-5",
    "anthropic:claude-haiku-4-5",
]

# Representative user messages, one per path. Single-turn; no tool calls
# expected. We're measuring model wall-clock to first reply, not tool flow.
SCENARIOS: List[Dict[str, str]] = [
    {
        "name": "greeting",
        "path": "general",
        "user": "Hi, can you tell me a bit about what McInnis & Holloway offers?",
    },
    {
        "name": "pricing",
        "path": "general",
        "user": "How much does a basic cremation cost?",
    },
    {
        "name": "preplan",
        "path": "pre_need",
        "user": "I'd like to plan my own funeral arrangements ahead of time. Where do I start?",
    },
    {
        "name": "immediate_need",
        "path": "immediate_need",
        "user": "My mother passed away last night. I need help arranging her funeral.",
    },
    {
        "name": "obituary",
        "path": "obituary",
        "user": "Do you have any recent obituaries for the Smith family?",
    },
]


async def load_prompts(location_slug: str) -> Dict[str, str]:
    """Build the full merged system prompt for each scenario path."""
    async with async_session_factory() as db:
        # Resolve org=mhc + location.
        org_row = (
            await db.execute(select(Organization).where(Organization.slug == "mhc"))
        ).scalar_one_or_none()
        if not org_row:
            raise RuntimeError("Organization 'mhc' not found")
        loc_row = (
            await db.execute(
                select(Location).where(
                    Location.organization_id == org_row.id,
                    Location.id == location_slug,
                )
            )
        ).scalar_one_or_none()
        if not loc_row:
            raise RuntimeError(f"Location '{location_slug}' not found for org mhc")

        out: Dict[str, str] = {}
        seen: Dict[str, str] = {}
        for sc in SCENARIOS:
            path = sc["path"]
            if path in seen:
                out[sc["name"]] = seen[path]
                continue
            prompt = await build_system_prompt(db, location=loc_row, path=path)
            seen[path] = prompt
            out[sc["name"]] = prompt
        return out


async def call_openai(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    system_prompt: str,
    user_msg: str,
    max_tokens: int,
) -> Dict[str, Any]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    t0 = time.monotonic()
    r = await client.post(
        "https://api.openai.com/v1/chat/completions",
        json=body, headers=headers, timeout=120.0,
    )
    dt_ms = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        return {"ok": False, "status": r.status_code, "error": r.text[:300], "duration_ms": dt_ms}
    data = r.json()
    usage = data.get("usage") or {}
    text = ""
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except Exception:
        pass
    return {
        "ok": True,
        "duration_ms": dt_ms,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "reply": text,
    }


async def call_anthropic(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    system_prompt: str,
    user_msg: str,
    max_tokens: int,
) -> Dict[str, Any]:
    body = {
        "model": model,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_msg}],
        "max_tokens": max_tokens,
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    t0 = time.monotonic()
    r = await client.post(
        "https://api.anthropic.com/v1/messages",
        json=body, headers=headers, timeout=120.0,
    )
    dt_ms = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        return {"ok": False, "status": r.status_code, "error": r.text[:300], "duration_ms": dt_ms}
    data = r.json()
    usage = data.get("usage") or {}
    text = ""
    try:
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
    except Exception:
        pass
    return {
        "ok": True,
        "duration_ms": dt_ms,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reply": text,
    }


async def run_one(
    client: httpx.AsyncClient,
    spec: str,
    system_prompt: str,
    user_msg: str,
    *,
    openai_key: str,
    anthropic_key: str,
    max_tokens: int,
) -> Dict[str, Any]:
    if ":" not in spec:
        raise ValueError(f"model spec must be 'provider:name', got {spec}")
    provider, model = spec.split(":", 1)
    if provider == "openai":
        if not openai_key:
            return {"ok": False, "error": "OPENAI_API_KEY missing", "duration_ms": 0}
        return await call_openai(client, openai_key, model, system_prompt, user_msg, max_tokens)
    if provider == "anthropic":
        if not anthropic_key:
            return {"ok": False, "error": "ANTHROPIC_API_KEY missing", "duration_ms": 0}
        return await call_anthropic(client, anthropic_key, model, system_prompt, user_msg, max_tokens)
    raise ValueError(f"unknown provider {provider}")


def _fmt_ms(values: List[int]) -> str:
    if not values:
        return "—"
    if len(values) == 1:
        return f"{values[0]}"
    return f"{int(statistics.mean(values))} (med {int(statistics.median(values))})"


async def main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    openai_key = settings.openai_api_key or os.environ.get("OPENAI_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not openai_key:
        print("WARN: OPENAI_API_KEY not set — OpenAI rows will fail.", file=sys.stderr)
    if not anthropic_key:
        print("WARN: ANTHROPIC_API_KEY not set — Anthropic rows will fail.", file=sys.stderr)

    models = [m.strip() for m in args.models.split(",") if m.strip()]

    print(f"loading merged Sarah prompts (location={args.location})…")
    prompts = await load_prompts(args.location)
    # Print prompt sizes for context.
    print("\nprompt sizes (chars):")
    seen_paths: Dict[str, int] = {}
    for sc in SCENARIOS:
        if sc["path"] not in seen_paths:
            seen_paths[sc["path"]] = len(prompts[sc["name"]])
    for path, size in seen_paths.items():
        print(f"  {path:<18} {size}")

    # results[model][scenario] = list of result dicts
    results: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        m: {sc["name"]: [] for sc in SCENARIOS} for m in models
    }

    async with httpx.AsyncClient() as client:
        for run_idx in range(args.runs):
            print(f"\n══ run {run_idx + 1}/{args.runs} ══════════════════════════════════════════════")
            for sc in SCENARIOS:
                print(f"\n  scenario={sc['name']:<16} path={sc['path']}")
                for model in models:
                    res = await run_one(
                        client, model,
                        system_prompt=prompts[sc["name"]],
                        user_msg=sc["user"],
                        openai_key=openai_key,
                        anthropic_key=anthropic_key,
                        max_tokens=args.max_tokens,
                    )
                    results[model][sc["name"]].append(res)
                    if res.get("ok"):
                        print(
                            f"    {model:<35} {res['duration_ms']:>6}ms  "
                            f"in={res.get('input_tokens')}  out={res.get('output_tokens')}"
                        )
                    else:
                        print(f"    {model:<35} FAIL {res.get('status')}: {str(res.get('error'))[:120]}")

    # Aggregate.
    print("\n\n══ summary (mean ms over all scenarios × runs) ══════════════════════════")
    print(f"{'model':<35} {'avg ms':>8} {'med ms':>8} {'min':>6} {'max':>6} {'n ok':>5} {'n fail':>6}")
    summary_rows: List[Dict[str, Any]] = []
    for m in models:
        all_durs: List[int] = []
        n_fail = 0
        for sc in SCENARIOS:
            for r in results[m][sc["name"]]:
                if r.get("ok"):
                    all_durs.append(int(r["duration_ms"]))
                else:
                    n_fail += 1
        if all_durs:
            avg = int(statistics.mean(all_durs))
            med = int(statistics.median(all_durs))
            mn, mx = min(all_durs), max(all_durs)
        else:
            avg = med = mn = mx = 0
        n_ok = len(all_durs)
        summary_rows.append({"model": m, "avg_ms": avg, "med_ms": med, "min_ms": mn, "max_ms": mx, "n_ok": n_ok, "n_fail": n_fail})
        print(f"{m:<35} {avg:>8} {med:>8} {mn:>6} {mx:>6} {n_ok:>5} {n_fail:>6}")

    # Per-scenario per-model mean.
    print("\n══ per-scenario mean ms ════════════════════════════════════════════════")
    header = f"{'scenario':<16} " + " ".join(f"{m:>22}" for m in models)
    print(header)
    for sc in SCENARIOS:
        cells = []
        for m in models:
            durs = [int(r["duration_ms"]) for r in results[m][sc["name"]] if r.get("ok")]
            cells.append(f"{int(statistics.mean(durs)):>22}" if durs else f"{'—':>22}")
        print(f"{sc['name']:<16} " + " ".join(cells))

    # Dump raw to JSON for the report.
    out_path = args.out
    with open(out_path, "w") as f:
        json.dump(
            {
                "location": args.location,
                "runs": args.runs,
                "max_tokens": args.max_tokens,
                "models": models,
                "scenarios": SCENARIOS,
                "prompt_sizes": seen_paths,
                "summary": summary_rows,
                "raw": results,
            },
            f, indent=2, default=str,
        )
    print(f"\nwrote raw results: {out_path}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Sarah latency bench — simple cross-provider.")
    p.add_argument("--location", default="park_memorial")
    p.add_argument("--runs", type=int, default=3, help="Repeats per (model, scenario).")
    p.add_argument("--max-tokens", type=int, default=400)
    p.add_argument("--models", default=",".join(DEFAULT_MODELS))
    p.add_argument("--out", default="bench_simple_results.json")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
