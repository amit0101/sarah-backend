"""Sarah latency benchmark harness — session 20.

Drives Sarah via the live /api/chat/message endpoint with a representative
synthetic suite, then reads back per-round wall-clock latency + token
usage from the `_meta` block we now stash on `sarah.openai_response_logs.payload`
(see `app/conversation_engine/engine.py`).

The OpenAI model is selected by the OPENAI_MODEL env var on the *backend*
process — this script does not (and cannot) override the model per-request.
Workflow per candidate model:

    cd backend
    # 1. start backend with the model pinned
    OPENAI_MODEL=gpt-4o      .venv/bin/uvicorn app.main:app --port 8000
    # 2. in another shell, run the bench
    .venv/bin/python -m scripts.bench_latency --tag gpt-4o
    # 3. stop the backend, restart with the next model, repeat
    OPENAI_MODEL=gpt-4o-mini .venv/bin/uvicorn app.main:app --port 8000
    .venv/bin/python -m scripts.bench_latency --tag gpt-4o-mini

Each run writes `bench_results_<tag>.json` in the cwd. After all runs:

    .venv/bin/python -m scripts.bench_latency --aggregate \
        bench_results_gpt-4o.json \
        bench_results_gpt-4o-mini.json \
        bench_results_gpt-4.1-mini.json

…prints a markdown comparison table to stdout.

The suite covers the 6 scenarios called out in `SESSION_20_START_PROMPT.md`:
  1. greeting / generic info       (general path, 1 turn)
  2. pricing                       (general path, 1 turn — must redirect to estimator)
  3. preplanning intro             (pre_need path, 1 turn)
  4. immediate-need triage         (immediate_need path, 1 turn)
  5. obituary search               (obituary path, 1 turn — exercises search_obituary tool)
  6. at-need booking flow          (immediate_need path, 3 turns — exercises check_calendar +
                                    book_appointment tool round-trips)

Total: 8 turns. Each scenario starts a fresh conversation so model wall-clock
is not contaminated by prior-turn context length growth.
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
from uuid import UUID

import httpx
from sqlalchemy import select

from app.database.session import async_session_factory
from app.models.openai_response_log import OpenAIResponseLog

DEFAULT_BASE = os.environ.get("SARAH_BASE_URL", "http://localhost:8000")
ORG_SLUG = "mhc"
DEFAULT_LOCATION = "park_memorial"


SCENARIOS: List[Dict[str, Any]] = [
    {
        "name": "greeting_general",
        "expected_path": "general",
        "turns": ["Hi, can you tell me a bit about what McInnis & Holloway offers?"],
    },
    {
        "name": "pricing_redirect",
        "expected_path": "general",
        # Per session-18 prompt rules: must redirect to estimatemhfh.com, never quote a range.
        "turns": ["How much does a basic cremation cost with you?"],
    },
    {
        "name": "preplanning_intro",
        "expected_path": "pre_need",
        "turns": ["I'd like to plan my own funeral arrangements ahead of time. Where do I start?"],
    },
    {
        "name": "immediate_need_triage",
        "expected_path": "immediate_need",
        "turns": ["My mother passed away last night. I need help arranging her funeral."],
    },
    {
        "name": "obituary_search",
        "expected_path": "obituary",
        "turns": ["Do you have any recent obituaries for the Smith family?"],
    },
    {
        "name": "atneed_booking_flow",
        "expected_path": "immediate_need",
        "turns": [
            (
                "Hi, my father passed yesterday. I'm Bench Tester, phone 403-555-0199, "
                "email bench.tester@example.com. We'd like to come in tomorrow morning "
                "to Park Memorial."
            ),
            "Yes, tomorrow morning works. What time do you have?",
            "Thanks — I'll think it over and call back.",
        ],
    },
]


async def _post_message(
    client: httpx.AsyncClient,
    base: str,
    *,
    location_id: str,
    message: str,
    conversation_id: Optional[str],
) -> Tuple[Dict[str, Any], float]:
    body: Dict[str, Any] = {
        "organization_slug": ORG_SLUG,
        "location_id": location_id,
        "message": message,
    }
    if conversation_id:
        body["conversation_id"] = conversation_id
    t0 = time.monotonic()
    r = await client.post(f"{base}/api/chat/message", json=body, timeout=180.0)
    wall_ms = (time.monotonic() - t0) * 1000.0
    r.raise_for_status()
    return r.json(), wall_ms


async def _fetch_rounds(conversation_id: UUID) -> List[Dict[str, Any]]:
    """Return per-round metadata from `_meta` on the persisted payload, oldest-first."""
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
        meta = payload.get("_meta") or {}
        out.append(
            {
                "round_index": row.round_index,
                "openai_response_id": row.openai_response_id,
                "duration_ms": meta.get("duration_ms"),
                "model": meta.get("model"),
                "tools_count": meta.get("tools_count"),
                "has_file_search": meta.get("has_file_search"),
                "path": meta.get("path"),
                "num_function_calls": meta.get("num_function_calls"),
                "usage": meta.get("usage"),
            }
        )
    return out


async def run_scenario(
    client: httpx.AsyncClient,
    base: str,
    location: str,
    scenario: Dict[str, Any],
) -> Dict[str, Any]:
    print(f"\n── scenario: {scenario['name']} ─────────────────────────────")
    conv_id: Optional[str] = None
    turns_out: List[Dict[str, Any]] = []
    for i, user_text in enumerate(scenario["turns"], 1):
        print(f"  [u{i}] {user_text[:120]}")
        try:
            resp, wall_ms = await _post_message(
                client, base,
                location_id=location, message=user_text, conversation_id=conv_id,
            )
        except httpx.HTTPStatusError as e:
            print(f"  ERROR {e.response.status_code}: {e.response.text[:200]}")
            return {
                "name": scenario["name"],
                "error": f"http_{e.response.status_code}",
                "turns": turns_out,
            }
        conv_id = resp["conversation_id"]
        reply = (resp.get("reply") or "").replace("\n", " ")
        print(f"  [s{i} {wall_ms:.0f}ms total] {reply[:160]}")
        turns_out.append(
            {
                "turn_index": i,
                "user_text": user_text,
                "reply": resp.get("reply", ""),
                "client_wall_ms": int(wall_ms),
            }
        )
    if not conv_id:
        return {"name": scenario["name"], "error": "no_conversation_id", "turns": turns_out}

    rounds = await _fetch_rounds(UUID(conv_id))
    return {
        "name": scenario["name"],
        "expected_path": scenario["expected_path"],
        "conversation_id": conv_id,
        "turns": turns_out,
        "rounds": rounds,
    }


async def cmd_run(base: str, location: str, tag: str, out_path: str) -> int:
    print(f"sarah_base_url: {base}")
    print(f"location:       {location}")
    print(f"tag:            {tag}")

    # Sanity: tickle /health to confirm the backend is up before we start.
    async with httpx.AsyncClient() as client:
        try:
            await client.get(f"{base}/", timeout=5.0)
        except Exception as e:
            print(f"  WARN backend not reachable at {base}: {e}")

        results: List[Dict[str, Any]] = []
        for scenario in SCENARIOS:
            res = await run_scenario(client, base, location, scenario)
            results.append(res)

    # Pull the model name off the first round we observed. The operator is
    # responsible for setting OPENAI_MODEL on the backend; we just record what
    # actually got used.
    observed_model: Optional[str] = None
    for r in results:
        for rd in (r.get("rounds") or []):
            if rd.get("model"):
                observed_model = rd["model"]
                break
        if observed_model:
            break

    payload = {
        "tag": tag,
        "observed_model": observed_model,
        "base": base,
        "location": location,
        "scenarios": results,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"\nwrote: {out_path}  (observed_model={observed_model})")
    _print_run_summary(payload)
    return 0


def _round_durations(payload: Dict[str, Any]) -> List[int]:
    """Flatten all per-round duration_ms across all scenarios."""
    out: List[int] = []
    for sc in payload.get("scenarios") or []:
        for rd in (sc.get("rounds") or []):
            d = rd.get("duration_ms")
            if isinstance(d, (int, float)):
                out.append(int(d))
    return out


def _first_round_durations(payload: Dict[str, Any]) -> List[int]:
    """First-round-per-turn duration_ms — best proxy for perceived latency
    on simple turns where Sarah replies without any tool round-trip."""
    out: List[int] = []
    for sc in payload.get("scenarios") or []:
        rounds = sc.get("rounds") or []
        # round_index resets per turn; collect every round_index==0 row.
        for rd in rounds:
            if rd.get("round_index") == 0:
                d = rd.get("duration_ms")
                if isinstance(d, (int, float)):
                    out.append(int(d))
    return out


def _client_walls(payload: Dict[str, Any]) -> List[int]:
    out: List[int] = []
    for sc in payload.get("scenarios") or []:
        for t in sc.get("turns") or []:
            w = t.get("client_wall_ms")
            if isinstance(w, (int, float)):
                out.append(int(w))
    return out


def _input_output_tokens(payload: Dict[str, Any]) -> Tuple[List[int], List[int]]:
    inp: List[int] = []
    outp: List[int] = []
    for sc in payload.get("scenarios") or []:
        for rd in (sc.get("rounds") or []):
            u = rd.get("usage") or {}
            it = u.get("input_tokens")
            ot = u.get("output_tokens")
            if isinstance(it, int):
                inp.append(it)
            if isinstance(ot, int):
                outp.append(ot)
    return inp, outp


def _pct(values: List[int], q: float) -> Optional[int]:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round((len(s) - 1) * q))))
    return s[idx]


def _print_run_summary(payload: Dict[str, Any]) -> None:
    rounds = _round_durations(payload)
    walls = _client_walls(payload)
    inp, outp = _input_output_tokens(payload)
    print("\n── summary ──")
    print(f"  rounds observed:     {len(rounds)}")
    print(f"  per-round duration:  median={_pct(rounds, 0.5)}ms  p95={_pct(rounds, 0.95)}ms  max={max(rounds) if rounds else None}ms")
    print(f"  per-turn client wall: median={_pct(walls, 0.5)}ms  p95={_pct(walls, 0.95)}ms  max={max(walls) if walls else None}ms")
    if inp:
        print(f"  input tokens/round:  median={_pct(inp, 0.5)}  p95={_pct(inp, 0.95)}  max={max(inp)}")
    if outp:
        print(f"  output tokens/round: median={_pct(outp, 0.5)}  p95={_pct(outp, 0.95)}  max={max(outp)}")


def cmd_aggregate(paths: List[str]) -> int:
    runs: List[Dict[str, Any]] = []
    for p in paths:
        with open(p) as f:
            runs.append(json.load(f))

    # Per-run aggregate table.
    print("# Sarah latency benchmark — comparison\n")
    print("Generated by `scripts/bench_latency.py --aggregate`. Per-round = single OpenAI Responses API call. Per-turn = total time from user POST to final reply (includes tool round-trips).\n")
    print("| tag | observed_model | scenarios | rounds | round median | round p95 | turn median | turn p95 | input tok median | output tok median |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in runs:
        rounds = _round_durations(r)
        walls = _client_walls(r)
        inp, outp = _input_output_tokens(r)
        n_sc = len(r.get("scenarios") or [])
        print(
            f"| {r.get('tag')} | {r.get('observed_model')} | {n_sc} | {len(rounds)} | "
            f"{_pct(rounds, 0.5)} | {_pct(rounds, 0.95)} | "
            f"{_pct(walls, 0.5)} | {_pct(walls, 0.95)} | "
            f"{_pct(inp, 0.5)} | {_pct(outp, 0.5)} |"
        )

    # Per-scenario, per-tag breakdown — single-round scenarios only (clearer
    # apples-to-apples; multi-round scenarios are dominated by tool latency).
    print("\n## Per-scenario per-round latency (ms)\n")
    scenario_names = [s["name"] for s in SCENARIOS]
    header = "| scenario | " + " | ".join(r.get("tag", "?") for r in runs) + " |"
    sep = "|---|" + "|".join(["---:"] * len(runs)) + "|"
    print(header)
    print(sep)
    for sn in scenario_names:
        row = [sn]
        for r in runs:
            sc = next((s for s in (r.get("scenarios") or []) if s.get("name") == sn), None)
            if not sc:
                row.append("—")
                continue
            durs = [
                rd["duration_ms"] for rd in (sc.get("rounds") or [])
                if isinstance(rd.get("duration_ms"), (int, float))
            ]
            if not durs:
                row.append("—")
            else:
                row.append(f"med {_pct(durs, 0.5)} / max {max(durs)} / n={len(durs)}")
        print("| " + " | ".join(row) + " |")

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Sarah latency benchmark harness (session 20).")
    p.add_argument("--base", default=DEFAULT_BASE, help="Sarah backend base URL.")
    p.add_argument("--location", default=DEFAULT_LOCATION, help="Location slug to drive against.")
    p.add_argument("--tag", default=None, help="Run tag — embedded in output filename and aggregate table.")
    p.add_argument("--out", default=None, help="Output JSON path. Default: bench_results_<tag>.json")
    p.add_argument("--aggregate", action="store_true", help="Aggregate prior bench_results_*.json files; remaining args are paths.")
    p.add_argument("paths", nargs="*", help="When --aggregate, the JSON files to merge.")
    args = p.parse_args(argv)

    if args.aggregate:
        if not args.paths:
            print("--aggregate requires at least one input JSON path", file=sys.stderr)
            return 2
        return cmd_aggregate(args.paths)

    tag = args.tag or os.environ.get("OPENAI_MODEL") or "untagged"
    out_path = args.out or f"bench_results_{tag}.json"
    return asyncio.run(cmd_run(args.base, args.location, tag, out_path))


if __name__ == "__main__":
    sys.exit(main())
