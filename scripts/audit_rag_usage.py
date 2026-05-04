"""Audit Sarah's file_search (RAG) usage rate from openai_response_logs."""
from __future__ import annotations

import asyncio
import os
from dotenv import load_dotenv

load_dotenv()
import asyncpg


async def main() -> None:
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://").replace("+asyncpg", "")
    conn = await asyncpg.connect(url, statement_cache_size=0)
    try:
        print("═══ last 7 days: total rounds vs rounds that invoked file_search ═══")
        row = await conn.fetchrow(
            """
            SELECT
              count(*) AS total_rounds,
              count(*) FILTER (WHERE payload::text LIKE '%"type": "file_search_call"%') AS with_fs,
              count(DISTINCT conversation_id) AS conversations
            FROM sarah.openai_response_logs
            WHERE created_at > now() - interval '7 days'
            """
        )
        if row:
            t, fs, c = row["total_rounds"], row["with_fs"], row["conversations"]
            pct = (fs / t * 100) if t else 0.0
            print(f"  total_rounds={t}  with_file_search={fs} ({pct:.1f}%)  distinct_conversations={c}")

        print()
        print("═══ per-day breakdown (last 14d) ═══")
        rows = await conn.fetch(
            """
            SELECT
              date_trunc('day', created_at)::date AS day,
              count(*) AS total,
              count(*) FILTER (WHERE payload::text LIKE '%"type": "file_search_call"%') AS with_fs
            FROM sarah.openai_response_logs
            WHERE created_at > now() - interval '14 days'
            GROUP BY 1 ORDER BY 1 DESC
            """
        )
        for r in rows:
            t = r["total"] or 0
            fs = r["with_fs"] or 0
            pct = (fs / t * 100) if t else 0.0
            print(f"  {r['day']}  total={t:>4}  with_fs={fs:>4}  ({pct:>5.1f}%)")

        print()
        print("═══ sample of recent file_search calls (queries the model issued) ═══")
        # Pull the queries out of file_search_call output items.
        rows = await conn.fetch(
            """
            SELECT created_at, conversation_id,
                   (o->'queries')::text AS queries
            FROM sarah.openai_response_logs,
                 jsonb_array_elements(payload->'output') o
            WHERE o->>'type' = 'file_search_call'
              AND created_at > now() - interval '7 days'
            ORDER BY created_at DESC
            LIMIT 30
            """
        )
        for r in rows:
            qs = r["queries"] or ""
            if len(qs) > 200:
                qs = qs[:200] + "…"
            print(f"  {r['created_at'].isoformat(timespec='seconds')}  conv={str(r['conversation_id'])[:8]}…  {qs}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
