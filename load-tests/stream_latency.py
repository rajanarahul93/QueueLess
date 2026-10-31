"""Local demo tool: N customer streams and one staff stream; staff actions; delivery delays.

Run from `backend/` with the Compose stack up: `uv run python ../load-tests/stream_latency.py 50`
(N defaults to 50). It reuses booking_race.py: owner sign-up and the customer helper that refuses
to run unless APP_ENV is "local" and never prints tokens. Each round is call-next, start and
complete; the delay is from an action's response to each affected customer's event. It prints
counts and percentiles only.
"""

import asyncio
import json
import statistics
import sys
import time
from typing import Any

import httpx
from booking_race import API, mint_customers, owner_headers

ROUNDS = 5


async def received(stream: "Stream", since: float) -> float:
    await stream.event("token.updated")
    return time.perf_counter() - since


class Stream:
    def __init__(self, response: httpx.Response) -> None:
        self.lines = response.aiter_lines()

    async def event(self, kind: str) -> dict[str, Any]:
        found: dict[str, Any] = {}
        async for line in self.lines:
            if line.startswith("event: "):
                found["event"] = line[7:]
            elif line.startswith("data: "):
                found["data"] = json.loads(line[6:])
            elif not line and found.get("event") == kind:
                return found
            elif not line:
                found = {}
        raise EOFError(kind)


async def main(count: int) -> int:
    limits = httpx.Limits(max_connections=count * 2 + 20)
    async with httpx.AsyncClient(base_url=API, timeout=60, limits=limits) as client:
        owner = await owner_headers(client)
        every_day = {d: [{"open": "00:00", "close": "23:59"}] for d in "mon tue wed thu fri sat sun".split()}
        business = await client.post(
            "/businesses",
            headers=owner,
            json={"name": "Latency", "category": "demo", "timezone": "UTC", "open_hours": every_day},
        )
        business.raise_for_status()
        business_id = business.json()["id"]
        settings = await client.patch(
            f"/businesses/{business_id}", headers=owner, json={"settings": {"max_walk_ins": 1000}}
        )
        settings.raise_for_status()
        queue_id = (await client.get(f"/businesses/{business_id}/queue", headers=owner)).json()["id"]
        service = await client.post(
            f"/businesses/{business_id}/services",
            headers=owner,
            json={"name": "Visit", "duration_min": 10, "price": "1.00"},
        )
        service.raise_for_status()
        service_id = service.json()["id"]
        people = mint_customers(count)
        tokens = []
        for headers in people:
            joined = await client.post(f"/queues/{queue_id}/tokens", json={}, headers=headers)
            joined.raise_for_status()
            detail = await client.get(f"/tokens/{joined.json()['id']}", headers=headers)
            tokens.append(detail.json())
        staff_token = (await client.post(f"/queues/{queue_id}/stream-token", headers=owner)).json()
        async with client.stream(
            "GET", f"/queues/{queue_id}/stream?stream_token={staff_token['stream_token']}"
        ) as staff_response:
            staff = Stream(staff_response)
            await staff.event("snapshot")
            streams: list[tuple[httpx.Response, Stream]] = []
            contexts = []
            for token in tokens:
                context = client.stream(
                    "GET", f"/tokens/{token['id']}/stream?stream_token={token['stream_token']}"
                )
                response = await context.__aenter__()
                contexts.append(context)
                stream = Stream(response)
                await stream.event("snapshot")
                streams.append((response, stream))
            delays: list[float] = []
            try:
                for round_no in range(ROUNDS):
                    called = await client.post(f"/queues/{queue_id}/call-next", headers=owner)
                    acted = time.perf_counter()
                    called.raise_for_status()
                    waiting = [stream for _, stream in streams[round_no:]]  # all see a change
                    delays += await asyncio.gather(*(received(s, acted) for s in waiting))
                    token = called.json()
                    mine = streams[round_no][1]
                    body = {"version": token["version"], "service_id": service_id}
                    started = await client.post(f"/tokens/{token['id']}/start", json=body, headers=owner)
                    acted = time.perf_counter()
                    started.raise_for_status()
                    delays.append(await received(mine, acted))
                    body = {"version": started.json()["version"]}
                    done = await client.post(f"/tokens/{token['id']}/complete", json=body, headers=owner)
                    acted = time.perf_counter()
                    done.raise_for_status()
                    delays.append(await received(mine, acted))
            finally:
                for context in reversed(contexts):
                    await context.__aexit__(None, None, None)
    delays.sort()
    p50 = statistics.median(delays)
    p95 = delays[max(0, int(len(delays) * 0.95) - 1)]
    print(
        f"N={count}: customer streams={count}, staff stream=1, events measured={len(delays)}, "
        f"p50={p50 * 1000:.0f} ms, p95={p95 * 1000:.0f} ms, max={delays[-1] * 1000:.0f} ms"
    )
    return 0 if p95 < 1.0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 50)))
