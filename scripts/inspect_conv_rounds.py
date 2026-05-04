"""Inspect per-round breakdown of recent live conversations."""
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()
import asyncpg


async def main() -> None:
    prefixes = sys.argv[1:] or ["32b555af", "0311d5b9", "5f7489c2", "d96ca0c3"]
    url = (
        os.environ["DATABASE_URL"]
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("+asyncpg", "")
    )
    conn = await asyncpg.connect(url, statement_cache_size=0)
    try:
        for prefix in prefixes:
            rows = await conn.fetch(
                """
                SELECT round_index,
                       payload->>'model' AS model,
                       (SELECT count(*) FROM jsonb_array_elements(payload->'output') o
                          WHERE o->>'type' = 'function_call') AS fn_calls,
                       (SELECT count(*) FROM jsonb_array_elements(payload->'output') o
                          WHERE o->>'type' = 'file_search_call') AS fs_calls,
                       payload->'usage'->>'input_tokens'  AS in_tok,
                       payload->'usage'->>'output_tokens' AS out_tok,
                       created_at
                FROM sarah.openai_response_logs
                WHERE conversation_id::text LIKE $1
                ORDER BY created_at
                """,
                prefix + "%",
            )
            print(f"\nconv {prefix}: {len(rows)} round(s)")
            prev_ts = None
            for r in rows:
                d = dict(r)
                ts = d["created_at"]
                gap = ""
                if prev_ts:
                    gap_s = (ts - prev_ts).total_seconds()
                    gap = f"  (+{gap_s:.2f}s since prev)"
                print(
                    f"  round={d['round_index']}  model={d['model']}  "
                    f"fn={d['fn_calls']}  fs={d['fs_calls']}  "
                    f"in={d['in_tok']}  out={d['out_tok']}  "
                    f"at={ts.isoformat(timespec='milliseconds')}{gap}"
                )
                prev_ts = ts
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
