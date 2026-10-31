"""Local demo tool: N waiting tokens, then N call-next presses at once from the owner and staff.

Run from `backend/` with the Compose stack up: `uv run python ../load-tests/call_next_race.py`
(N defaults to 100). It reuses booking_race.py: owner sign-up and the customer helper that refuses
to run unless APP_ENV is "local" and never prints tokens. It prints counts only, and whether every
waiting token was called exactly once.
"""

import asyncio
import sys
import uuid
from collections import Counter

import httpx
from booking_race import API, PASSWORD, mint_customers, owner_headers

STAFF = 4


async def staff_headers(client: httpx.AsyncClient, owner: dict[str, str], business_id: str) -> dict[str, str]:
    email = f"staff-{uuid.uuid4().hex[:10]}@example.test"
    created = await client.post(
        f"/businesses/{business_id}/staff",
        headers=owner,
        json={"name": "Staff", "email": email, "initial_password": PASSWORD},
    )
    created.raise_for_status()
    logged_in = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    logged_in.raise_for_status()
    return {"Authorization": f"Bearer {logged_in.json()['access_token']}"}


async def main(count: int) -> int:
    limits = httpx.Limits(max_connections=count * 2)
    async with httpx.AsyncClient(base_url=API, timeout=120, limits=limits) as client:
        owner = await owner_headers(client)
        every_day = {d: [{"open": "00:00", "close": "23:59"}] for d in "mon tue wed thu fri sat sun".split()}
        business = await client.post(
            "/businesses",
            headers=owner,
            json={"name": "Call race", "category": "demo", "timezone": "UTC", "open_hours": every_day},
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
        joins = await asyncio.gather(
            *(client.post(f"/queues/{queue_id}/tokens", json={}, headers=h) for h in mint_customers(count))
        )
        waiting = {r.json()["id"] for r in joins if r.status_code == 201}
        team = [owner, *[await staff_headers(client, owner, business_id) for _ in range(STAFF)]]
        calls = await asyncio.gather(
            *(client.post(f"/queues/{queue_id}/call-next", headers=team[i % len(team)]) for i in range(count))
        )
    counts = Counter(
        str(r.status_code) if r.status_code < 300 else r.json().get("code", str(r.status_code))
        for r in calls
    )
    called = [r.json()["id"] for r in calls if r.status_code == 200]
    exact = len(waiting) == count and sorted(called) == sorted(waiting)
    print(f"N={count}, {len(team)} accounts: joins={len(waiting)} calls={dict(counts)} "
          f"every waiting token called exactly once: {exact}")
    return 0 if exact else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 100)))
