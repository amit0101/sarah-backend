"""Probe prod model name and current latency."""
import asyncio, os, time
import httpx
from dotenv import load_dotenv
load_dotenv()
import asyncpg

async def main():
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=180.0) as c:
        r = await c.post(
            "https://sarah-backend-lqoy.onrender.com/api/chat/message",
            json={"organization_slug": "mhc", "location_id": "park_memorial",
                  "message": "Hi, model check please. One short sentence."},
        )
    dt = int((time.monotonic() - t0) * 1000)
    d = r.json()
    cid = d.get("conversation_id")
    print(f"wall={dt}ms  conv={cid}")
    print(f"reply={(d.get('reply') or '')[:160]}")
    url = (
        os.environ["DATABASE_URL"]
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("+asyncpg", "")
    )
    conn = await asyncpg.connect(url, statement_cache_size=0)
    rows = await conn.fetch(
        """SELECT round_index, payload->>'model' AS m,
                  payload->'usage'->>'input_tokens' AS in_tok
           FROM sarah.openai_response_logs
           WHERE conversation_id=$1::uuid ORDER BY created_at""",
        cid,
    )
    for r in rows:
        d2 = dict(r)
        print(f"  round={d2['round_index']}  model={d2['m']}  in_tok={d2['in_tok']}")
    await conn.close()

asyncio.run(main())
