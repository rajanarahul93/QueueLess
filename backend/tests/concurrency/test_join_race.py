"""Gap-free token numbers under concurrency (NFR-2) through the real HTTP app, Postgres, Redis.

Every request is launched at once on the client (asyncio.gather). On the server, at most
pool_size + max_overflow transactions hold a database connection at the same time; the rest
wait for one. The queue row lock then serializes the joins themselves. Each run prints how many
requests were in flight on the client and how many connections were checked out at most.
Any 500 fails a test.
"""

import asyncio
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import event, select

from app.models import Queue, QueueToken
from tests.concurrency.test_booking_race import SqlStates, customers
from tests.integration.api import Api
from tests.integration.test_availability import CLOCK, utc
from tests.integration.test_booking import World, book_one, local, world
from tests.integration.test_queues import queue_id
from tests.integration.test_tokens import settings

pytestmark = pytest.mark.anyio


@dataclass
class Load:
    """Client requests in flight, and database connections checked out, at most."""

    in_flight: int = 0
    max_in_flight: int = 0
    checked_out: int = 0
    max_checked_out: int = 0
    statuses: Counter[str] = field(default_factory=Counter)

    def outcome(self, response: httpx.Response) -> None:
        assert response.status_code != 500, response.text
        self.statuses[
            str(response.status_code)
            if response.status_code < 300
            else response.json().get("code", str(response.status_code))
        ] += 1


async def tracked(load: Load, request: Any) -> httpx.Response:
    load.in_flight += 1
    load.max_in_flight = max(load.max_in_flight, load.in_flight)
    try:
        response: httpx.Response = await request
    finally:
        load.in_flight -= 1
    load.outcome(response)
    return response


def watch_pool(api: Api, load: Load) -> tuple[Any, Any]:
    pool = api.app.state.engine.sync_engine.pool

    def out(*args: object) -> None:
        load.checked_out += 1
        load.max_checked_out = max(load.max_checked_out, load.checked_out)

    def back(*args: object) -> None:
        load.checked_out -= 1

    event.listen(pool, "checkout", out)
    event.listen(pool, "checkin", back)
    return out, back


def unwatch_pool(api: Api, handlers: tuple[Any, Any]) -> None:
    pool = api.app.state.engine.sync_engine.pool
    event.remove(pool, "checkout", handlers[0])
    event.remove(pool, "checkin", handlers[1])


def capacity(api: Api) -> int:
    pool = api.app.state.engine.sync_engine.pool
    return int(pool.size() + pool._max_overflow)


async def numbers(api: Api, queue: str) -> tuple[list[int], int]:
    async with api.sessions() as session:
        nos = list(
            (
                await session.scalars(
                    select(QueueToken.token_no)
                    .where(QueueToken.queue_id == uuid.UUID(queue))
                    .order_by(QueueToken.token_no)
                )
            ).all()
        )
        last = await session.scalar(select(Queue.last_token_no).where(Queue.id == uuid.UUID(queue)))
    return nos, int(last or 0)


def join(api: Api, headers: dict[str, str], queue: str) -> Any:
    return api.client.post(f"/v1/queues/{queue}/tokens", json={}, headers=headers)


@contextmanager
def measured(api: Api, load: Load) -> Iterator[SqlStates]:
    handlers = watch_pool(api, load)
    try:
        with SqlStates(api) as states:
            yield states
    finally:
        unwatch_pool(api, handlers)


def report(
    capsys: pytest.CaptureFixture[str], label: str, api: Api, load: Load, states: SqlStates
) -> None:
    with capsys.disabled():
        print(
            f"\n{label}: {dict(load.statuses)} 23505={states.counts['23505']} "
            f"40P01={states.counts['40P01']} other_sqlstate="
            f"{sum(n for k, n in states.counts.items() if k not in ('23505', '40P01'))} "
            f"client_in_flight_max={load.max_in_flight} "
            f"db_connections_max={load.max_checked_out}/{capacity(api)}"
        )


async def fresh_queue(api: Api, max_walk_ins: int = 1000) -> tuple[World, str]:
    w = await world(api)
    await settings(w, max_walk_ins=max_walk_ins)
    return w, await queue_id(w)


async def test_five_hundred_joins_number_exactly_one_to_five_hundred(
    api: Api, capsys: pytest.CaptureFixture[str]
) -> None:
    for run in range(3):
        _, queue = await fresh_queue(api)
        heads = await customers(api, 500)
        load = Load()
        with measured(api, load) as states:
            await asyncio.gather(*(tracked(load, join(api, h, queue)) for h in heads))
        report(capsys, f"500 joins run {run + 1}", api, load, states)
        assert load.statuses == {"201": 500}
        nos, last = await numbers(api, queue)
        assert nos == list(range(1, 501)) and last == 500


async def test_fifty_joins_from_one_customer_take_one_number(
    api: Api, capsys: pytest.CaptureFixture[str]
) -> None:
    _, queue = await fresh_queue(api)
    (headers,) = await customers(api, 1)
    load = Load()
    with measured(api, load) as states:
        await asyncio.gather(*(tracked(load, join(api, headers, queue)) for _ in range(50)))
    report(capsys, "50 joins, one customer", api, load, states)
    assert load.statuses == {"201": 1, "ALREADY_IN_QUEUE": 49}
    assert await numbers(api, queue) == ([1], 1)  # 49 rollbacks returned their number


async def test_numbers_stay_contiguous_when_some_joins_fail(
    api: Api, capsys: pytest.CaptureFixture[str]
) -> None:
    _, queue = await fresh_queue(api)
    holders = await customers(api, 10)
    for headers in holders:
        assert (await join(api, headers, queue)).status_code == 201
    fresh = await customers(api, 40)
    load = Load()
    with measured(api, load) as states:
        await asyncio.gather(*(tracked(load, join(api, h, queue)) for h in holders + fresh))
    report(capsys, "40 fresh + 10 duplicates", api, load, states)
    assert load.statuses == {"201": 40, "ALREADY_IN_QUEUE": 10}
    assert await numbers(api, queue) == (list(range(1, 51)), 50)


async def test_joins_racing_a_pause(api: Api, capsys: pytest.CaptureFixture[str]) -> None:
    w, queue = await fresh_queue(api)
    heads = await customers(api, 50)
    load = Load()
    pause = api.client.patch(f"/v1/queues/{queue}", json={"status": "paused"}, headers=w.owner)
    with measured(api, load) as states:
        joins = [tracked(load, join(api, h, queue)) for h in heads]
        results = await asyncio.gather(*joins[:25], pause, *joins[25:])  # pause mid-stream
    report(capsys, "50 joins racing a pause", api, load, states)
    assert results[25].status_code == 200
    assert set(load.statuses) <= {"201", "QUEUE_CLOSED"}
    created = load.statuses["201"]
    assert await numbers(api, queue) == (list(range(1, created + 1)), created)
    (late,) = await customers(api, 1)
    assert (await join(api, late, queue)).json()["code"] == "QUEUE_CLOSED"


async def test_max_walk_ins_is_exact_under_concurrency(
    api: Api, capsys: pytest.CaptureFixture[str]
) -> None:
    _, queue = await fresh_queue(api, max_walk_ins=10)
    heads = await customers(api, 50)
    load = Load()
    with measured(api, load) as states:
        await asyncio.gather(*(tracked(load, join(api, h, queue)) for h in heads))
    report(capsys, "50 joins, max_walk_ins 10", api, load, states)
    assert load.statuses == {"201": 10, "WALK_INS_FULL": 40}
    assert await numbers(api, queue) == (list(range(1, 11)), 10)


def check_in(api: Api, headers: dict[str, str], appointment_id: str) -> Any:
    return api.client.post(f"/v1/appointments/{appointment_id}/check-in", headers=headers)


async def test_ten_simultaneous_check_ins_make_one_token(api: Api) -> None:
    w = await world(api)
    (headers,) = await customers(api, 1)
    appointment_id = await book_one(w, headers)
    api.clock.now = utc(9).timestamp()
    load = Load()
    responses = await asyncio.gather(
        *[tracked(load, check_in(api, headers, appointment_id)) for _ in range(10)]
    )
    assert load.statuses == {"201": 1, "200": 9}
    assert len({r.json()["id"] for r in responses}) == 1


async def test_check_in_racing_a_cancel_or_a_reschedule(api: Api) -> None:
    w = await world(api)
    await settings(w, reschedule_cutoff_min=0)
    for rival in ("cancel", "reschedule"):
        for attempt in range(5):
            (headers,) = await customers(api, 1)
            hour = 9 + attempt
            api.clock.now = CLOCK.timestamp()  # book before the slot, then move to its window
            appointment_id = await book_one(w, headers, hour)
            api.clock.now = (utc(hour) - timedelta(minutes=10)).timestamp()
            other = (
                api.client.delete(f"/v1/appointments/{appointment_id}", headers=headers)
                if rival == "cancel"
                else api.client.patch(
                    f"/v1/appointments/{appointment_id}/reschedule",
                    json={"start": local(hour, 15, day=9)},
                    headers=headers,
                )
            )
            checked, raced = await asyncio.gather(check_in(api, headers, appointment_id), other)
            assert 500 not in (checked.status_code, raced.status_code)
            wins = [checked.status_code == 201, raced.status_code in (200, 204)]
            assert wins.count(True) == 1, (rival, checked.text, raced.text)
            if checked.status_code == 201:  # keep the customer free for the next round
                await api.client.delete(f"/v1/tokens/{checked.json()['id']}", headers=headers)


async def test_check_ins_racing_walk_in_joins_keep_numbers_contiguous(
    api: Api, capsys: pytest.CaptureFixture[str]
) -> None:
    w = await world(api)
    await settings(w, max_walk_ins=1000)
    (booker,) = await customers(api, 1)
    appointment_id = await book_one(w, booker)
    api.clock.now = (utc(9) - timedelta(minutes=10)).timestamp()  # Monday: today's queue
    queue = await queue_id(w)
    walkers = await customers(api, 40)
    load = Load()
    with measured(api, load) as states:
        await asyncio.gather(
            tracked(load, check_in(api, booker, appointment_id)),
            *(tracked(load, join(api, h, queue)) for h in walkers),
        )
    report(capsys, "1 check-in racing 40 joins", api, load, states)
    assert load.statuses == {"201": 41}
    assert await numbers(api, queue) == (list(range(1, 42)), 41)
