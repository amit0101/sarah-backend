"""Quick prod latency probe — 6 messages, report steady-state."""
import asyncio, statistics, time
import httpx

MSGS = [
    "Hi, can you tell me a bit about what McInnis & Holloway offers?",
    "How can I learn more about your services?",
    "I would like to understand pricing.",
    "My father just passed. I need to arrange things.",
    "I would like to plan ahead for myself.",
    "Do you have a location near downtown?",
]


async def main() -> None:
    durs: list[int] = []
    async with httpx.AsyncClient(timeout=180.0) as c:
        for i, m in enumerate(MSGS):
            t0 = time.monotonic()
            r = await c.post(
                "https://sarah-backend-lqoy.onrender.com/api/chat/message",
                json={
                    "organization_slug": "mhc",
                    "location_id": "park_memorial",
                    "message": m,
                },
            )
            dt = int((time.monotonic() - t0) * 1000)
            durs.append(dt)
            d = r.json()
            cid = (d.get("conversation_id") or "")[:8]
            print(f"  {i + 1}/{len(MSGS)}  {dt:>6}ms  conv={cid}")
    steady = durs[1:]
    print(f"\nfirst (warmup): {durs[0]}ms")
    print(
        f"steady-state (n={len(steady)}):  "
        f"avg={int(statistics.mean(steady))}ms  "
        f"med={int(statistics.median(steady))}ms  "
        f"min={min(steady)}  max={max(steady)}"
    )


if __name__ == "__main__":
    asyncio.run(main())
