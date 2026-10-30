"""A REAL deadlock during a booking (Postgres 40P01), to prove the retry path runs.

The test's own transaction X raises its deadlock_timeout to 60 s, so Postgres always picks the
booking (default 1 s) as the victim. The 1 s is Postgres's own deadlock check, not a test sleep;
the test only polls pg_locks to know when each side is waiting.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncConnection

from app.models import Appointment, User
from tests.concurrency.test_booking_race import SqlStates
from tests.integration.api import Api
from tests.integration.test_availability import utc
from tests.integration.test_booking import World, customer, local, world

pytestmark = pytest.mark.anyio


async def waiters(api: Api) -> int:
    async with api.sessions() as probe:  # a fresh transaction per poll; pg_locks is not cached
        return int(await probe.scalar(text("SELECT count(*) FROM pg_locks WHERE NOT granted")) or 0)


async def until_waiting(api: Api, count: int) -> None:
    for _ in range(20000):
        if await waiters(api) >= count:
            return
        await asyncio.sleep(0)
    pytest.fail(f"never saw {count} waiting lock(s)")


async def hold_slot(x: AsyncConnection, w: World, other: User) -> None:
    await x.execute(text("SET LOCAL deadlock_timeout = '60s'"))
    await x.execute(
        insert(Appointment).values(
            business_id=w.business_id,
            service_id=uuid.UUID(w.service_id),
            resource_id=w.resource,
            customer_id=other.id,
            during=Range(utc(9), utc(9, 40), bounds="[)"),
            status="booked",
        )
    )


@pytest.mark.parametrize(("x_commits", "expected"), [(True, 409), (False, 201)])
async def test_deadlocked_booking_retries_and_ends_cleanly(
    api: Api, x_commits: bool, expected: int
) -> None:
    w = await world(api)
    booker, headers = await customer(api)
    other, _ = await customer(api)
    engine = api.app.state.engine
    async with engine.connect() as x:
        await x.begin()
        await hold_slot(x, w, other)  # X holds slot 09:00, uncommitted
        with SqlStates(api) as states:
            booking = asyncio.create_task(w.book(headers, local(9)))
            await until_waiting(api, 1)  # B holds the customer row and waits on X's slot
            lock = asyncio.create_task(
                x.execute(select(User.id).where(User.id == booker.id).with_for_update())
            )
            await lock  # B was the deadlock victim; its rollback released the customer row
            await until_waiting(api, 1)  # B's retry now waits on the customer row X holds
            await (x.commit() if x_commits else x.rollback())
            response = await booking
    assert response.status_code == expected, response.text
    if expected == 409:
        assert response.json()["code"] == "SLOT_TAKEN"
    assert states.counts["40P01"] == 1, states.counts
    assert states.counts["23P01"] == (1 if x_commits else 0), states.counts
    print(f"\nx_commits={x_commits}: status={response.status_code} counts={dict(states.counts)}")
