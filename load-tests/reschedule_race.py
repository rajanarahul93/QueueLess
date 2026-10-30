"""Local demo tool: 20 customers reschedule their own appointments into ONE slot at once.

Run from `backend/` with the Compose stack up: `uv run python ../load-tests/reschedule_race.py`.
It reuses booking_race.py: the same owner and business setup, and the same customer helper that
refuses to run unless APP_ENV is "local" and never prints tokens. Each of 5 rounds mints 20 new
customers, books each one its own slot (45 minutes apart, none overlapping), then sends all 20
reschedules into the same target slot. It prints counts only; exactly one must win per round.
"""

import asyncio
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
from booking_race import API, setup_business, mint_customers

MOVERS = 20
ROUNDS = 5


async def main() -> int:
    limits = httpx.Limits(max_connections=MOVERS * 2)
    failures = 0
    async with httpx.AsyncClient(base_url=API, timeout=60, limits=limits) as client:
        first_day = (datetime.now(UTC) + timedelta(days=2)).date()
        ids = await setup_business(client, first_day)
        for round_no in range(ROUNDS):
            day = first_day + timedelta(days=round_no)
            midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
            customers = mint_customers(MOVERS)
            olds = []
            for index, headers in enumerate(customers):
                start = (midnight + timedelta(minutes=45 * index)).isoformat()
                booked = await client.post("/appointments", json=ids | {"start": start}, headers=headers)
                booked.raise_for_status()
                olds.append(booked.json()["id"])
            target = (midnight + timedelta(hours=17)).isoformat()
            responses = await asyncio.gather(
                *(
                    client.patch(
                        f"/appointments/{old}/reschedule", json={"start": target}, headers=headers
                    )
                    for old, headers in zip(olds, customers, strict=True)
                )
            )
            counts = Counter(
                "moved" if r.status_code == 200
                else "SLOT_TAKEN" if r.json().get("code") == "SLOT_TAKEN"
                else "other"
                for r in responses
            )
            print(
                f"round {round_no + 1}: moved={counts['moved']} "
                f"SLOT_TAKEN={counts['SLOT_TAKEN']} other={counts['other']}"
            )
            failures += counts["moved"] != 1 or counts["other"] != 0
    print("OK" if failures == 0 else f"FAILED rounds: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
