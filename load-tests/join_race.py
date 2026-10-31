"""Local demo tool: N customers join one walk-in queue at once; token numbers must be 1..N.

Run from `backend/` with the Compose stack up: `uv run python ../load-tests/join_race.py 500`
(N defaults to 100). It reuses booking_race.py: owner sign-up and the customer helper that refuses
to run unless APP_ENV is "local" and never prints tokens. It prints counts only.
"""

import asyncio
import sys
from collections import Counter

import httpx
from booking_race import API, mint_customers, owner_headers


async def main(count: int) -> int:
    limits = httpx.Limits(max_connections=count * 2)
    async with httpx.AsyncClient(base_url=API, timeout=120, limits=limits) as client:
        owner = await owner_headers(client)
        every_day = {d: [{"open": "00:00", "close": "23:59"}] for d in "mon tue wed thu fri sat sun".split()}
        business = await client.post(
            "/businesses",
            headers=owner,
            json={"name": "Join race", "category": "demo", "timezone": "UTC", "open_hours": every_day},
        )
        business.raise_for_status()
        business_id = business.json()["id"]
        cap = await client.patch(
            f"/businesses/{business_id}", headers=owner, json={"settings": {"max_walk_ins": 1000}}
        )
        cap.raise_for_status()
        queue = await client.get(f"/businesses/{business_id}/queue", headers=owner)
        queue.raise_for_status()
        queue_id = queue.json()["id"]
        customers = mint_customers(count)
        responses = await asyncio.gather(
            *(client.post(f"/queues/{queue_id}/tokens", json={}, headers=h) for h in customers)
        )
    counts = Counter(
        "created" if r.status_code == 201 else r.json().get("code", str(r.status_code))
        for r in responses
    )
    numbers = sorted(r.json()["token_no"] for r in responses if r.status_code == 201)
    contiguous = numbers == list(range(1, count + 1))
    print(f"N={count}: {dict(counts)} numbers exactly 1..{count}: {contiguous}")
    return 0 if contiguous and counts == {"created": count} else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 100)))
