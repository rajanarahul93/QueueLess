"""The nightly rollup into daily_stats against hand-computed rows. A fresh database per test: the
job scans every business."""

import uuid
from datetime import date, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import insert, select, update

from app.models import DailyStat, QueueToken, ServiceSession
from app.services import rollups
from tests.integration.api import Api, new_email
from tests.integration.db import alembic_config, reset_schema
from tests.integration.test_booking import World, world
from tests.integration.test_jobs import past_queue

pytestmark = pytest.mark.anyio

DAY = date(2026, 10, 30)  # a Friday


@pytest.fixture
def migrated_url(postgres_url: str) -> str:
    reset_schema(postgres_url)
    command.upgrade(alembic_config(postgres_url), "head")
    return postgres_url


def local(day: date, clock: str, offset: str = "+05:30") -> datetime:
    return datetime.fromisoformat(f"{day.isoformat()}T{clock}{offset}")


async def token(
    api: Api,
    w: World,
    queue: str,
    joined: datetime,
    status: str,
    started: datetime | None = None,
    predicted: int | None = None,
    queued: datetime | None = None,
) -> None:
    """A token of the world's service (through its session when it started)."""
    person = await api.create_user("customer", email=new_email())
    async with api.sessions() as session:
        number = 1 + len(
            (
                await session.scalars(
                    select(QueueToken.id).where(QueueToken.queue_id == uuid.UUID(queue))
                )
            ).all()
        )
        token_id = await session.scalar(
            insert(QueueToken)
            .values(
                business_id=w.business_id,
                queue_id=uuid.UUID(queue),
                token_no=number,
                source="walk_in",
                customer_id=person.id,
                status=status,
                joined_at=joined,
                queued_at=queued or joined,
                started_at=started,
                predicted_wait_sec=predicted,
            )
            .returning(QueueToken.id)
        )
        if started is not None:
            session.add(
                ServiceSession(
                    business_id=w.business_id,
                    token_id=token_id,
                    service_id=uuid.UUID(w.service_id),
                    resource_id=w.resource,
                    started_at=started,
                )
            )
        await session.commit()


async def rows(api: Api, w: World) -> dict[tuple[object, date, int | None], tuple[object, ...]]:
    """(service "S" or None, day, hour) -> (joined, served, no_shows, cancelled, avg, p90,
    predicted)."""
    async with api.sessions() as session:
        found = (
            await session.scalars(select(DailyStat).where(DailyStat.business_id == w.business_id))
        ).all()
    return {
        ("S" if row.service_id else None, row.stat_date, row.hour): (
            row.tokens_joined,
            row.tokens_served,
            row.no_shows,
            row.cancelled,
            row.avg_wait_sec,
            row.p90_wait_sec,
            row.avg_predicted_wait_sec,
        )
        for row in found
    }


async def seeded_day(api: Api, w: World, day: date = DAY) -> None:
    queue = await past_queue(w, day)
    await token(api, w, queue, local(day, "09:10"), "completed", local(day, "09:30"), 900)
    await token(api, w, queue, local(day, "09:20"), "completed", local(day, "09:50"), 1500)
    await token(api, w, queue, local(day, "09:45"), "in_service", local(day, "10:00"), 600)
    await token(api, w, queue, local(day, "10:05"), "no_show")  # never started: no service
    await token(api, w, queue, local(day, "10:40"), "cancelled")


AFTER = local(DAY + timedelta(days=1), "01:00")
# Hour 9 of the service: 3 joined, 2 served (the in-service one started but is not served),
# waits 1200, 1800, 900 -> mean 1300; 3 waits < 5 -> no p90; predicted (900+1500+600)/3 = 1000.
# Hour 10 without a service: 2 joined, 1 no-show, 1 cancelled, no waits.
EXPECTED = {
    ("S", DAY, 9): (3, 2, 0, 0, 1300, None, 1000),
    ("S", DAY, None): (3, 2, 0, 0, 1300, None, 1000),
    (None, DAY, 10): (2, 0, 1, 1, None, None, None),
    (None, DAY, None): (2, 0, 1, 1, None, None, None),
}


async def test_rows_match_the_hand_computed_day(api: Api) -> None:
    w = await world(api)
    await seeded_day(api, w)
    summary = await rollups.rollup(api.sessions, AFTER)
    assert (summary.processed, summary.changed, summary.skipped) == (1, 1, 0)
    assert await rows(api, w) == EXPECTED


async def test_p90_needs_five_waits(api: Api) -> None:
    w = await world(api)
    queue = await past_queue(w, DAY)
    for minutes in (1, 2, 3, 4, 5):  # waits 60..300 s, all joined in hour 9
        joined = local(DAY, f"09:0{minutes}")
        await token(api, w, queue, joined, "completed", joined + timedelta(minutes=minutes))
    await rollups.rollup(api.sessions, AFTER)
    # percentile_cont(0.9) of 60, 120, 180, 240, 300: position 0.9 x 4 = 3.6 -> 240 + 0.6 x 60
    assert (await rows(api, w))[("S", DAY, None)][4:6] == (180, 276)
    other = await world(api)
    queue = await past_queue(other, DAY)
    for minutes in (1, 2, 3, 4):
        joined = local(DAY, f"09:0{minutes}")
        await token(api, other, queue, joined, "completed", joined + timedelta(minutes=minutes))
    await rollups.rollup(api.sessions, AFTER)
    assert (await rows(api, other))[("S", DAY, None)][4:6] == (150, None)  # 4 waits


async def test_a_second_run_gives_the_same_rows_and_a_late_change_is_picked_up(
    api: Api,
) -> None:
    w = await world(api)
    await seeded_day(api, w)
    await rollups.rollup(api.sessions, AFTER)
    await rollups.rollup(api.sessions, AFTER + timedelta(hours=1))
    assert await rows(api, w) == EXPECTED
    async with api.sessions() as session:  # the in-service token is completed after midnight
        await session.execute(
            update(QueueToken).where(QueueToken.status == "in_service").values(status="completed")
        )
        await session.commit()
    await rollups.rollup(api.sessions, AFTER + timedelta(hours=2))
    assert (await rows(api, w))[("S", DAY, None)][:2] == (3, 3)  # rewritten, not added


async def test_catch_up_covers_the_last_three_complete_days_only(api: Api) -> None:
    w = await world(api)
    for back in range(5):  # 30 Oct back to 26 Oct
        queue = await past_queue(w, DAY - timedelta(days=back))
        joined = local(DAY - timedelta(days=back), "11:00")
        await token(api, w, queue, joined, "cancelled")
    today = await past_queue(w, DAY + timedelta(days=1))  # 31 Oct: not complete at 01:00
    await token(api, w, today, local(DAY + timedelta(days=1), "00:30"), "cancelled")
    summary = await rollups.rollup(api.sessions, AFTER)
    assert summary.processed == 3
    assert sorted({day for _, day, _ in await rows(api, w)}) == [
        DAY - timedelta(days=2),
        DAY - timedelta(days=1),
        DAY,
    ]


async def test_the_budget_leaves_the_rest_for_the_next_run_never_rolled_first(
    api: Api,
) -> None:
    w = await world(api)
    days = [DAY - timedelta(days=back) for back in (2, 1, 0)]
    for day in days:
        await token(api, w, await past_queue(w, day), local(day, "11:00"), "cancelled")
    ticks = iter(range(100))
    clock = lambda: float(next(ticks))  # noqa: E731 - each read is one second later

    first = await rollups.rollup(api.sessions, AFTER, budget_sec=2, timer=clock)
    assert (first.processed, first.skipped) == (1, 2)
    assert {day for _, day, _ in await rows(api, w)} == {days[0]}  # the oldest
    ticks = iter(range(100))
    second = await rollups.rollup(api.sessions, AFTER, budget_sec=2, timer=clock)
    assert (second.processed, second.skipped) == (1, 2)
    # never-rolled days come before the one already rolled
    assert {day for _, day, _ in await rows(api, w)} == {days[0], days[1]}


async def test_each_business_gets_only_its_own_rows(api: Api) -> None:
    w, other = await world(api), await world(api)
    await seeded_day(api, w)
    queue = await past_queue(other, DAY)
    await token(api, other, queue, local(DAY, "12:00"), "no_show")
    await rollups.rollup(api.sessions, AFTER)
    assert await rows(api, w) == EXPECTED
    assert await rows(api, other) == {
        (None, DAY, 12): (1, 0, 1, 0, None, None, None),
        (None, DAY, None): (1, 0, 1, 0, None, None, None),
    }


async def test_new_york_dst_days_and_kolkata_hours(api: Api) -> None:
    ny = await world(api, zone="America/New_York")
    fall = date(2026, 11, 1)  # 01:00-01:59 happens twice
    queue = await past_queue(ny, fall)
    await token(api, ny, queue, local(fall, "01:30", "-04:00"), "cancelled")  # EDT
    await token(api, ny, queue, local(fall, "01:30", "-05:00"), "cancelled")  # EST
    spring = date(2026, 3, 8)  # 02:00-02:59 does not exist
    queue = await past_queue(ny, spring)
    await token(api, ny, queue, local(spring, "01:59", "-05:00"), "cancelled")
    await token(api, ny, queue, local(spring, "03:00", "-04:00"), "cancelled")
    await rollups.rollup(api.sessions, local(date(2026, 3, 9), "01:00", "-04:00"))
    await rollups.rollup(api.sessions, local(date(2026, 11, 2), "01:00", "-05:00"))
    found = await rows(api, ny)
    assert {hour for _, day, hour in found if day == fall and hour is not None} == {1}
    assert found[(None, fall, 1)][0] == 2  # both 01:30s in one bucket
    assert {hour for _, day, hour in found if day == spring and hour is not None} == {1, 3}
    kolkata = await world(api)
    queue = await past_queue(kolkata, DAY)
    await token(api, kolkata, queue, local(DAY, "00:15"), "cancelled")  # 18:45 UTC the day before
    await token(api, kolkata, queue, local(DAY, "23:59"), "cancelled")
    await rollups.rollup(api.sessions, AFTER)
    hours = {hour for _, _, hour in await rows(api, kolkata) if hour is not None}
    assert hours == {0, 23}


async def test_the_wait_counts_from_joining_not_from_the_place_in_the_queue(api: Api) -> None:
    """D4: a skip moves queued_at; the wait still starts when the customer joined."""
    w = await world(api)
    queue = await past_queue(w, DAY)
    joined = local(DAY, "09:00")
    await token(api, w, queue, joined, "completed", local(DAY, "09:30"), queued=local(DAY, "09:20"))
    await rollups.rollup(api.sessions, AFTER)
    assert (await rows(api, w))[("S", DAY, None)][4] == 1800  # not 600
