"""NFR-3: concurrent bookings through the real HTTP app, Postgres and Redis (Rules R6.4).

Every response must be a 201, a SLOT_TAKEN, or (never expected here) a 503 after exhausted
deadlock retries; a 500 or anything else fails the test. SQLSTATE counts come from the engine's
error hook, so the retried deadlocks are visible too.
"""

import asyncio
import uuid
from collections import Counter
from typing import Any

import httpx
import pytest
from sqlalchemy import event, func, select

from app.core.security import create_access_token
from app.models import Appointment
from tests.integration.api import Api
from tests.integration.test_booking import World, customer, local, world

pytestmark = pytest.mark.anyio

CONCURRENT = 50
RUNS = 5
ALL_DAY = {"mon": [{"open": "00:00", "close": "23:59"}]}


class SqlStates:
    """Counts database errors by SQLSTATE (23P01 exclusion, 40P01 deadlock) on one engine."""

    def __init__(self, api: Api) -> None:
        self.counts: Counter[str] = Counter()
        self._engine = api.app.state.engine.sync_engine

    def _on_error(self, context: Any) -> None:
        cause = getattr(context.original_exception, "__cause__", None)
        self.counts[str(getattr(cause, "sqlstate", "other"))] += 1

    def others(self) -> int:
        return sum(n for state, n in self.counts.items() if state not in ("23P01", "40P01"))

    def __enter__(self) -> "SqlStates":
        event.listen(self._engine, "handle_error", self._on_error)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self._engine, "handle_error", self._on_error)


async def customers(api: Api, count: int) -> list[dict[str, str]]:
    users = await asyncio.gather(
        *(api.create_user("customer", phone=_phone()) for _ in range(count))
    )
    return [
        {"Authorization": f"Bearer {create_access_token(api.settings, u.id, 'customer')}"}
        for u in users
    ]


def _phone() -> str:
    return f"+91{uuid.uuid4().int % 10**10:010d}"


async def fire(
    api: Api, w: World, jobs: list[tuple[dict[str, str], str, dict[str, str]]]
) -> list[httpx.Response]:
    return list(
        await asyncio.gather(
            *(w.book(headers, start, extra=extra) for headers, start, extra in jobs)
        )
    )


def statuses(responses: list[httpx.Response]) -> Counter[int]:
    return Counter(r.status_code for r in responses)


async def appointment_count(api: Api, w: World) -> int:
    async with api.sessions() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(Appointment)
                .where(Appointment.business_id == w.business_id)
            )
            or 0
        )


async def test_fifty_customers_one_slot_exactly_one_wins_five_times(
    api: Api, capsys: pytest.CaptureFixture[str]
) -> None:
    w = await world(api, open_hours=ALL_DAY)
    for run in range(RUNS):
        taken = local(9 + run)  # a different slot every run
        heads = await customers(api, CONCURRENT)
        with SqlStates(api) as states:
            responses = await fire(api, w, [(h, taken, {}) for h in heads])
        assert statuses(responses) == {201: 1, 409: CONCURRENT - 1}, statuses(responses)
        for response in responses:
            if response.status_code == 409:
                body = response.json()
                assert body["code"] == "SLOT_TAKEN"
                assert taken not in [slot["start"] for slot in body["next_slots"]]
                assert len(body["next_slots"]) <= 3
        with capsys.disabled():
            print(
                f"\nrun {run + 1}: 201={statuses(responses)[201]} "
                f"SLOT_TAKEN={statuses(responses)[409]} "
                f"23P01={states.counts['23P01']} 40P01={states.counts['40P01']} "
                f"other_sqlstate={states.others()}"
            )
        assert states.others() == 0
    assert await appointment_count(api, w) == RUNS  # one row per run


async def test_one_customer_one_idempotency_key_creates_one_appointment(api: Api) -> None:
    w = await world(api, open_hours=ALL_DAY)
    _, headers = await customer(api)
    responses = await fire(api, w, [(headers, local(9), {"Idempotency-Key": "race"})] * CONCURRENT)
    assert set(statuses(responses)) <= {201, 409}, statuses(responses)
    assert statuses(responses)[201] >= 1
    for response in responses:
        if response.status_code == 409:
            assert response.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert await appointment_count(api, w) == 1


async def test_fifty_different_slots_from_one_customer_respect_the_cap_exactly(api: Api) -> None:
    w = await world(api, open_hours=ALL_DAY)
    _, headers = await customer(api)
    # 45-minute steps are exactly one 30-minute service plus its 10-minute buffer, rounded to the
    # 15-minute grid, so no two of these 50 slots overlap each other (two Mondays, 25 each).
    jobs: list[tuple[dict[str, str], str, dict[str, str]]] = [
        (headers, local(*divmod(step * 45, 60), day=day), {})
        for day in (2, 9)
        for step in range(CONCURRENT // 2)
    ]
    responses = await fire(api, w, jobs)
    assert set(statuses(responses)) <= {201, 409}, statuses(responses)
    assert statuses(responses)[201] == 5
    assert {r.json()["code"] for r in responses if r.status_code == 409} == {
        "BOOKING_LIMIT_REACHED"
    }
    assert await appointment_count(api, w) == 5


async def test_overlapping_starts_from_different_customers_exactly_one_wins(api: Api) -> None:
    w = await world(api, open_hours=ALL_DAY)
    heads = await customers(api, CONCURRENT)
    # 30-minute service with a 10-minute buffer: starts 15 minutes apart all overlap in groups.
    starts = [local(9, 0), local(9, 15), local(9, 30)]
    responses = await fire(api, w, [(h, starts[i % 3], {}) for i, h in enumerate(heads)])
    assert set(statuses(responses)) <= {201, 409}, statuses(responses)
    # 09:00 [09:00,09:40) and 09:15 and 09:30 all overlap each other, so only one can exist.
    assert statuses(responses)[201] == 1
    assert await appointment_count(api, w) == 1


async def test_back_to_back_slots_both_succeed(api: Api) -> None:
    w = await world(api, open_hours=ALL_DAY, service={"buffer_min": 15})  # stored [09:00, 09:45)
    first, second = await customers(api, 2)
    responses = await fire(api, w, [(first, local(9), {}), (second, local(9, 45), {})])
    assert statuses(responses) == {201: 2}
