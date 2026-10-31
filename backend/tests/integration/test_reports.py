"""Owner reports through HTTP, checked against independent SQL over queue_tokens. Today is
Sunday 1 November (Kolkata): 30 October is rolled up, 31 October is not (read live), and today
is always live."""

import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import insert, select, text

from app.models import DailyStat, Queue
from app.services import rollups
from tests.integration.api import Api
from tests.integration.test_booking import World, admin, customer, world
from tests.integration.test_jobs import past_queue
from tests.integration.test_rollups import local, token

pytestmark = pytest.mark.anyio

TODAY = date(2026, 11, 1)
ROLLED, UNROLLED = TODAY - timedelta(days=2), TODAY - timedelta(days=1)
REPORTS = ("peak-hours", "wait-times", "no-shows")


async def report(api: Api, w: World, name: str, headers: Any = None, **params: str) -> Any:
    return await api.client.get(
        f"/v1/businesses/{w.business_id}/reports/{name}",
        params=params,
        headers=w.owner if headers is None else headers,
    )


async def seeded(api: Api) -> World:
    w = await world(api)
    for day in (ROLLED, UNROLLED, TODAY):
        queue = await past_queue(w, day)
        await token(api, w, queue, local(day, "09:00"), "completed", local(day, "09:20"), 900)
        await token(api, w, queue, local(day, "09:30"), "completed", local(day, "10:10"), 1800)
        await token(api, w, queue, local(day, "10:00"), "no_show")
        await token(api, w, queue, local(day, "13:00"), "cancelled")
    await roll(api, w, ROLLED)
    return w


async def roll(api: Api, w: World, day: date) -> None:
    async with api.sessions() as session:
        await rollups.roll_day(session, w.business_id, "Asia/Kolkata", day)
        await session.commit()


async def day_queue(api: Api, w: World, day: date) -> str:
    async with api.sessions() as session:
        found = await session.scalar(
            select(Queue.id).where(Queue.business_id == w.business_id, Queue.queue_date == day)
        )
    return str(found)


async def sql(api: Api, query: str, w: World) -> list[tuple[Any, ...]]:
    async with api.sessions() as session:
        rows = await session.execute(text(query), {"b": w.business_id})
        return [tuple(row) for row in rows.all()]


# The expected figures, written independently of the service code.
PEAK_SQL = """
SELECT extract(isodow FROM q.queue_date)::int - 1, extract(hour FROM t.joined_at AT TIME ZONE
    'Asia/Kolkata')::int, count(*)
FROM queue_tokens t JOIN queues q ON q.id = t.queue_id
WHERE q.business_id = :b GROUP BY 1, 2 ORDER BY 1, 2
"""
WAIT_SQL = """
SELECT q.queue_date, s.service_id::text,
    count(*) FILTER (WHERE t.status = 'completed'),
    round(avg(extract(epoch FROM t.started_at - t.joined_at)))::int,
    round(avg(t.predicted_wait_sec))::int
FROM queue_tokens t JOIN queues q ON q.id = t.queue_id JOIN service_sessions s ON s.token_id = t.id
WHERE q.business_id = :b GROUP BY 1, 2 ORDER BY 1, 2
"""
NO_SHOW_SQL = """
SELECT count(*), count(*) FILTER (WHERE t.status = 'no_show'),
    count(*) FILTER (WHERE t.status = 'cancelled')
FROM queue_tokens t JOIN queues q ON q.id = t.queue_id
WHERE q.business_id = :b AND t.started_at IS NULL
"""


async def test_reports_match_independent_sql_across_rolled_unrolled_and_today(
    api: Api,
) -> None:
    w = await seeded(api)
    peak = (await report(api, w, "peak-hours")).json()
    assert [(row["weekday"], row["hour"], row["tokens"]) for row in peak] == await sql(
        api, PEAK_SQL, w
    )
    waits = (await report(api, w, "wait-times")).json()
    assert [
        (
            date.fromisoformat(row["date"]),
            row["service_id"],
            row["served"],
            row["avg_wait_sec"],
            row["avg_predicted_wait_sec"],
        )
        for row in waits
    ] == await sql(api, WAIT_SQL, w)
    # per day: waits 1200 and 2400 -> 1800; 2 waits < 5 -> no p90
    assert {row["avg_wait_sec"] for row in waits} == {1800}
    assert {row["p90_wait_sec"] for row in waits} == {None}
    missed = (await report(api, w, "no-shows")).json()
    never_started = await sql(api, NO_SHOW_SQL, w)
    by_service = {row["service_id"]: row for row in missed}
    assert (
        by_service[None]["joined"],
        by_service[None]["no_shows"],
        by_service[None]["cancelled"],
    ) == never_started[0]
    assert by_service[None]["no_show_rate"] == by_service[None]["cancel_rate"] == 0.5
    assert by_service[w.service_id]["joined"] == 6  # 2 started tokens x 3 days


async def test_past_days_come_only_from_their_rollup_and_today_only_live(api: Api) -> None:
    w = await seeded(api)
    before = (await report(api, w, "no-shows")).json()
    # a token added to the rolled day after its rollup is not seen (the stored rows win)...
    rolled_queue = await day_queue(api, w, ROLLED)
    await token(api, w, rolled_queue, local(ROLLED, "15:00"), "no_show")
    # ...and a stored row for today (which the rollup never writes) is ignored, not added
    async with api.sessions() as session:
        await session.execute(
            insert(DailyStat).values(
                business_id=w.business_id,
                service_id=None,
                stat_date=TODAY,
                hour=None,
                tokens_joined=100,
                no_shows=100,
            )
        )
        await session.commit()
    assert (await report(api, w, "no-shows")).json() == before


async def test_a_day_without_rollup_rows_is_read_live_once(api: Api) -> None:
    w = await seeded(api)
    only = {"from": UNROLLED.isoformat(), "to": UNROLLED.isoformat()}
    live = (await report(api, w, "no-shows", **only)).json()
    await roll(api, w, UNROLLED)
    assert (await report(api, w, "no-shows", **only)).json() == live  # same numbers, once


async def test_a_report_read_during_a_rollup_sees_the_old_day_or_the_new_never_part(
    api: Api,
) -> None:
    w = await seeded(api)
    only = {"from": ROLLED.isoformat(), "to": ROLLED.isoformat()}
    old = (await report(api, w, "no-shows", **only)).json()
    await token(api, w, await day_queue(api, w, ROLLED), local(ROLLED, "16:00"), "no_show")
    async with api.sessions() as holder:  # the rollup's transaction, not yet committed
        await rollups.roll_day(holder, w.business_id, "Asia/Kolkata", ROLLED)
        assert (await report(api, w, "no-shows", **only)).json() == old  # a second connection
        await holder.commit()
    new = (await report(api, w, "no-shows", **only)).json()
    assert new != old
    assert {row["service_id"]: row["no_shows"] for row in new}[None] == 2


async def test_owner_only_with_the_owner_endpoint_check_order(api: Api) -> None:
    w = await world(api)
    other = await world(api)
    _, a_customer = await customer(api)
    staff = await w.staff()
    for name in REPORTS:
        assert (await report(api, w, name, headers={})).status_code == 401
        unknown = await api.client.get(
            f"/v1/businesses/{uuid.uuid4()}/reports/{name}", headers=w.owner
        )
        assert unknown.status_code == 404
        for headers in (a_customer, staff, await admin(api), other.owner):
            assert (await report(api, w, name, headers)).status_code == 403
        assert (await report(api, w, name)).json() == []  # no data: an empty list


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-11-01", "to": "2026-10-31"},  # from after to
        {"to": "2026-11-02"},  # after today
        {"from": "2026-07-31", "to": "2026-10-31"},  # 93 days
        {"from": "31-10-2026"},  # not a date
    ],
)
async def test_bad_ranges_are_422(api: Api, params: dict[str, str]) -> None:
    w = await world(api)
    assert (await report(api, w, "no-shows", **params)).status_code == 422


async def test_92_days_and_the_default_window_are_accepted(api: Api) -> None:
    w = await seeded(api)
    assert (await report(api, w, "no-shows", **{"from": "2026-08-02"})).status_code == 200
    everything = (await report(api, w, "peak-hours")).json()  # default: 3 Oct to today
    assert sum(row["tokens"] for row in everything) == 12
    older = (await report(api, w, "peak-hours", to=ROLLED.isoformat())).json()
    assert sum(row["tokens"] for row in older) == 4


async def test_rate_limit_is_30_per_minute_and_comes_before_422(api: Api) -> None:
    w = await world(api)
    for _ in range(30):
        assert (await report(api, w, "no-shows")).status_code == 200
    assert (await report(api, w, "no-shows", **{"from": "bad"})).status_code == 429


async def test_reports_hold_aggregates_only(api: Api) -> None:
    w = await seeded(api)
    keys = set()
    for name in REPORTS:
        for row in (await report(api, w, name)).json():
            keys |= set(row)
    assert keys == {
        "weekday",
        "hour",
        "tokens",
        "date",
        "service_id",
        "served",
        "avg_wait_sec",
        "p90_wait_sec",
        "avg_predicted_wait_sec",
        "joined",
        "no_shows",
        "cancelled",
        "no_show_rate",
        "cancel_rate",
    }
