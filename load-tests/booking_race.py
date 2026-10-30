"""Local demo tool: 50 customers book the same slot at once against the running Compose stack.

Run from the repository root with the stack up (`docker compose up -d`, migrations applied):

    python3 load-tests/booking_race.py

It registers an owner, creates a business and a service over HTTP, then books the same slot with
50 customers at once, 5 times with different slots, and prints only counts. Creating 50 customers
through the OTP endpoints would trip the per-IP OTP limits, so one `docker compose exec` helper
inserts them and mints their tokens inside the api container. That helper refuses to run unless
APP_ENV is "local". Tokens are passed over a pipe and never printed. Phase 7 will extend this.
Needs httpx (already a backend dev dependency): `cd backend && uv run python ../load-tests/...`.
"""

import asyncio
import json
import subprocess
import sys
import uuid
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

API = "http://localhost:8000/v1"
CUSTOMERS = 50
ROUNDS = 5
ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "load-test-password-1"

# Runs inside the api container (`python -`); the first argument is how many customers to make.
MINT_CUSTOMERS = """
import asyncio, json, os, sys, uuid
if os.environ.get("APP_ENV") != "local":
    sys.exit("refusing to create load-test customers: APP_ENV is not local")
from sqlalchemy import insert
from app.core.config import Settings
from app.core.security import create_access_token
from app.db.session import create_engine, create_session_factory
from app.models import User

async def main(count):
    settings = Settings()
    engine = create_engine(settings.database_url, 5)
    tokens = []
    async with create_session_factory(engine)() as session:
        for _ in range(count):
            phone = "+91" + str(uuid.uuid4().int % 10**10).zfill(10)
            user_id = await session.scalar(
                insert(User).values(name="Load customer", phone=phone, role="customer")
                .returning(User.id)
            )
            tokens.append(create_access_token(settings, user_id, "customer"))
        await session.commit()
    await engine.dispose()
    print(json.dumps(tokens))

asyncio.run(main(int(sys.argv[1])))
"""


def mint_customers(count: int) -> list[dict[str, str]]:
    done = subprocess.run(
        ["docker", "compose", "exec", "-T", "api", "python", "-", str(count)],
        input=MINT_CUSTOMERS,
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    if done.returncode != 0:
        sys.exit(f"could not create customers: {done.stderr.strip()[-300:]}")
    return [{"Authorization": f"Bearer {token}"} for token in json.loads(done.stdout)]


async def owner_headers(client: httpx.AsyncClient) -> dict[str, str]:
    email = f"load-{uuid.uuid4().hex[:10]}@example.test"
    body = {"name": "Load owner", "email": email, "password": PASSWORD}
    registered = await client.post("/auth/register", json=body)
    registered.raise_for_status()
    logged_in = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    logged_in.raise_for_status()
    return {"Authorization": f"Bearer {logged_in.json()['access_token']}"}


async def setup_business(client: httpx.AsyncClient, day: date) -> dict[str, str]:
    """Owner, business (UTC, open all day every day) and a 30-minute service with a 10-minute
    buffer; returns the ids a booking body needs."""
    owner = await owner_headers(client)
    every_day = {d: [{"open": "00:00", "close": "23:59"}] for d in "mon tue wed thu fri sat sun".split()}
    business = await client.post(
        "/businesses",
        headers=owner,
        json={"name": "Race", "category": "demo", "timezone": "UTC", "open_hours": every_day},
    )
    business.raise_for_status()
    business_id = business.json()["id"]
    service = await client.post(
        f"/businesses/{business_id}/services",
        headers=owner,
        json={"name": "Visit", "duration_min": 30, "buffer_min": 10, "price": "1.00"},
    )
    service.raise_for_status()
    availability = await client.get(
        f"/businesses/{business_id}/availability",
        params={"service_id": service.json()["id"], "date": day.isoformat()},
        headers=owner,
    )
    availability.raise_for_status()
    resource_id = availability.json()["slots"][0]["resource_id"]
    return {"service_id": service.json()["id"], "resource_id": resource_id}


async def main() -> int:
    day = (datetime.now(UTC) + timedelta(days=2)).date()
    limits = httpx.Limits(max_connections=CUSTOMERS * 2)
    async with httpx.AsyncClient(base_url=API, timeout=60, limits=limits) as client:
        ids = await setup_business(client, day)
        customers = mint_customers(CUSTOMERS)

        failures = 0
        for round_no in range(ROUNDS):
            start = datetime(day.year, day.month, day.day, 9 + round_no, tzinfo=UTC).isoformat()
            body = ids | {"start": start}
            responses = await asyncio.gather(
                *(client.post("/appointments", json=body, headers=h) for h in customers)
            )
            counts = Counter(
                "created" if r.status_code == 201
                else "SLOT_TAKEN" if r.json().get("code") == "SLOT_TAKEN"
                else "other"
                for r in responses
            )
            print(
                f"round {round_no + 1}: created={counts['created']} "
                f"SLOT_TAKEN={counts['SLOT_TAKEN']} other={counts['other']}"
            )
            failures += counts["created"] != 1 or counts["other"] != 0
        print("OK" if failures == 0 else f"FAILED rounds: {failures}")
        return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
