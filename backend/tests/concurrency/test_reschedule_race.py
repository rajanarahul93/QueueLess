"""Concurrent reschedules through the real HTTP app (FR-19). Any 500 fails a test."""

import asyncio
import uuid
from collections import Counter
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.models import Appointment
from tests.concurrency.test_booking_race import ALL_DAY, RUNS, SqlStates, customers
from tests.integration.api import Api
from tests.integration.test_booking import URL, World, local, world

pytestmark = pytest.mark.anyio

MOVERS = 20
NOVEMBER_MONDAYS = (2, 9, 16, 23, 30)


async def move(w: World, headers: dict[str, str], appointment_id: str, start: str) -> Any:
    return await w.api.client.patch(
        f"{URL}/{appointment_id}/reschedule", json={"start": start}, headers=headers
    )


async def book(w: World, headers: dict[str, str], start: str) -> str:
    response = await w.book(headers, start)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def snapshots(api: Api, ids: list[str]) -> dict[str, tuple[object, ...]]:
    async with api.sessions() as session:
        rows = await session.scalars(
            select(Appointment).where(Appointment.id.in_([uuid.UUID(i) for i in ids]))
        )
        return {str(r.id): (r.status, r.during, r.rescheduled_from_id) for r in rows}


async def successors(api: Api, old_id: str) -> int:
    async with api.sessions() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(Appointment)
                .where(Appointment.rescheduled_from_id == uuid.UUID(old_id))
            )
            or 0
        )


def codes(responses: list[httpx.Response]) -> Counter[str]:
    return Counter(
        "ok" if r.status_code < 300 else str(r.json().get("code", r.status_code)) for r in responses
    )


async def test_twenty_customers_into_one_slot_exactly_one_wins_five_times(
    api: Api, capsys: pytest.CaptureFixture[str]
) -> None:
    w = await world(api, open_hours=ALL_DAY)
    for run, day in enumerate(NOVEMBER_MONDAYS[:RUNS]):
        heads = await customers(api, MOVERS)
        # Each customer's own slot, 45 minutes apart so none overlap; the target is 17:00.
        olds = [await book(w, h, local(*divmod(i * 45, 60), day=day)) for i, h in enumerate(heads)]
        before = await snapshots(api, olds)
        target = local(17, day=day)
        with SqlStates(api) as states:
            responses = list(
                await asyncio.gather(
                    *(move(w, h, o, target) for h, o in zip(heads, olds, strict=True))
                )
            )
        assert codes(responses) == {"ok": 1, "SLOT_TAKEN": MOVERS - 1}, codes(responses)
        winner = next(o for o, r in zip(olds, responses, strict=True) if r.status_code == 200)
        after = await snapshots(api, olds)
        for old_id in olds:
            if old_id != winner:
                assert after[old_id] == before[old_id]  # every loser is untouched
        assert after[winner][0] == "rescheduled"
        for response in responses:
            if response.status_code == 409:
                assert target not in [s["start"] for s in response.json()["next_slots"]]
        with capsys.disabled():
            print(
                f"\nreschedule run {run + 1}: 200={codes(responses)['ok']} "
                f"SLOT_TAKEN={codes(responses)['SLOT_TAKEN']} "
                f"23P01={states.counts['23P01']} 40P01={states.counts['40P01']} "
                f"other_sqlstate={states.others()}"
            )
        assert states.others() == 0


async def test_simultaneous_reschedules_of_one_appointment_only_one_succeeds(api: Api) -> None:
    w = await world(api, open_hours=ALL_DAY)
    (headers,) = await customers(api, 1)
    old_id = await book(w, headers, local(9))
    targets = [local(10 + i) for i in range(10)]
    responses = list(await asyncio.gather(*(move(w, headers, old_id, t) for t in targets)))
    assert codes(responses) == {"ok": 1, "INVALID_TRANSITION": 9}, codes(responses)
    assert await successors(api, old_id) == 1


async def test_reschedule_racing_a_cancel_exactly_one_wins(api: Api) -> None:
    w = await world(api, open_hours=ALL_DAY)
    (headers,) = await customers(api, 1)
    for hour in range(9, 9 + RUNS):
        old_id = await book(w, headers, local(hour))
        moved, cancelled = await asyncio.gather(
            move(w, headers, old_id, local(hour, 15, day=9)),
            w.api.client.delete(f"{URL}/{old_id}", headers=headers),
        )
        outcome = (moved.status_code, cancelled.status_code)
        assert outcome in {(200, 409), (409, 204)}, outcome
        status = (await snapshots(api, [old_id]))[old_id][0]
        assert status == ("rescheduled" if moved.status_code == 200 else "cancelled")
        assert await successors(api, old_id) == (1 if moved.status_code == 200 else 0)
        if moved.status_code == 200:  # keep the customer under the cap
            await w.api.client.delete(f"{URL}/{moved.json()['id']}", headers=headers)


async def test_reschedule_racing_a_booking_for_the_same_slot(api: Api) -> None:
    w = await world(api, open_hours=ALL_DAY)
    for run in range(RUNS):
        mover, booker = await customers(api, 2)
        old_id = await book(w, mover, local(9 + run))
        target = local(15 + run)
        moved, booked = await asyncio.gather(move(w, mover, old_id, target), w.book(booker, target))
        assert {moved.status_code, booked.status_code} in ({200, 409}, {201, 409})
        loser = moved if moved.status_code == 409 else booked
        assert loser.json()["code"] == "SLOT_TAKEN"
        if moved.status_code == 409:
            assert (await snapshots(api, [old_id]))[old_id][0] == "booked"
