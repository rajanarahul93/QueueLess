"""ETA through the real database and Redis: tiers from stored sessions, call order with the
walk-in ratio, predicted_wait_sec, the per-queue cache and its invalidation. Expected values are
worked out by hand in the comments. The service in world() is 30 minutes (1800 s)."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy import event, insert, select

from app.models import OutboxEvent, QueueToken, ServiceSession
from app.services import queue as queue_service
from app.services import queue_jobs
from app.services.eta import Eta
from tests.integration.api import Api, new_email
from tests.integration.test_availability import CLOCK
from tests.integration.test_booking import World, book_one, customer, world
from tests.integration.test_jobs import ratio_queue
from tests.integration.test_queues import add_token, queue_id, snapshot
from tests.integration.test_serving import act, call_next, priority
from tests.integration.test_tokens import check_in, join, leave, set_status, token_row

pytestmark = pytest.mark.anyio

# CLOCK is 11:30 in Kolkata on Sunday 1 November: the hour band is 11, today starts at 00:00.
YESTERDAY_11 = CLOCK - timedelta(days=1)
TODAY_10 = CLOCK - timedelta(hours=1, minutes=30)


@contextmanager
def statements(api: Api) -> Iterator[list[str]]:
    seen: list[str] = []
    engine = api.app.state.engine.sync_engine

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def served(api: Api, w: World, queue: str, lengths: list[int], started: datetime) -> None:
    """Completed sessions of these lengths (seconds) of the world's service, a second apart."""
    person = await api.create_user("customer", name="Past", email=new_email())
    async with api.sessions() as session:
        first = await session.scalar(
            select(QueueToken.token_no)
            .where(QueueToken.queue_id == uuid.UUID(queue))
            .order_by(QueueToken.token_no.desc())
            .limit(1)
        )
        for index, length in enumerate(lengths):
            begin = started + timedelta(seconds=index)
            token_id = await session.scalar(
                insert(QueueToken)
                .values(
                    business_id=w.business_id,
                    queue_id=uuid.UUID(queue),
                    token_no=max(first or 0, 1000) + index + 1,
                    source="walk_in",
                    customer_id=person.id,
                    status="completed",
                    joined_at=begin,
                    queued_at=begin,
                    started_at=begin,
                    completed_at=begin + timedelta(seconds=length),
                )
                .returning(QueueToken.id)
            )
            session.add(
                ServiceSession(
                    business_id=w.business_id,
                    token_id=token_id,
                    service_id=uuid.UUID(w.service_id),
                    resource_id=w.resource,
                    started_at=begin,
                    ended_at=begin + timedelta(seconds=length),
                    duration_sec=length,
                )
            )
        await session.commit()


async def two_joined(api: Api, w: World) -> tuple[str, dict[str, object]]:
    queue = await queue_id(w)
    _, first = await customer(api)
    _, second = await customer(api)
    assert (await join(api, first, queue)).status_code == 201
    api.clock.now += 60
    response = await join(api, second, queue)
    assert response.status_code == 201, response.text
    return queue, response.json()


async def test_eta_in_every_customer_response_and_stored_at_join(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    _, first_headers = await customer(api)
    first = (await join(api, first_headers, queue)).json()
    assert first["eta"] == {"min_minutes": 0, "max_minutes": 2}  # nobody ahead
    assert (await token_row(api, first["id"])).predicted_wait_sec == 0
    api.clock.now += 60
    _, headers = await customer(api)
    second = (await join(api, headers, queue)).json()
    # no sessions yet: a walk-in takes the mean configured duration of the active services,
    # 1800 s, with -/+ 20% (360 s): 1440 to 2160 s = 24 to 36 min -> 20 to 40 (steps of 5)
    assert (second["ahead_count"], second["eta"]) == (1, {"min_minutes": 20, "max_minutes": 40})
    assert (await token_row(api, second["id"])).predicted_wait_sec == 1800
    detail = (await api.client.get(f"/v1/tokens/{second['id']}", headers=headers)).json()
    listed = (await api.client.get("/v1/tokens", params={"active": "true"}, headers=headers)).json()
    assert detail["eta"] == listed[0]["eta"] == second["eta"]
    called = await call_next(api, w.owner, queue)
    assert called.json()["id"] == first["id"]
    mine = (await api.client.get(f"/v1/tokens/{first['id']}", headers=first_headers)).json()
    assert (mine["status"], mine["ahead_count"], mine["eta"]) == ("called", None, None)


async def test_check_in_stores_its_prediction(api: Api) -> None:
    w = await world(api)
    _, headers = await customer(api)
    appointment_id = await book_one(w, headers)  # Monday 2 November 09:00 Kolkata
    api.clock.now = (datetime.fromisoformat("2026-11-02T08:40:00+05:30")).timestamp()
    queue = await queue_id(w)
    _, walker = await customer(api)
    assert (await join(api, walker, queue)).status_code == 201  # queued 08:40
    api.clock.now += 10 * 60
    body = (await check_in(api, headers, appointment_id)).json()
    # on time, so queued at the slot (09:00), behind the walk-in: one 1800 s token ahead
    assert (body["ahead_count"], body["eta"]) == (1, {"min_minutes": 20, "max_minutes": 40})
    assert (await token_row(api, body["id"])).predicted_wait_sec == 1800


async def test_stored_sessions_set_the_duration_and_outliers_never_count(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    # 10 sessions of 600 s in hour 11: tier 1 (and the walk-in tier) is 600 s with no spread.
    # 3 of 9000 s (over 4 x 1800 = 7200) and 2 of 20 s (under 30) are left out; were they
    # counted, the trimmed mean of 15 would drop one 20 and one 9000 and stop being 600.
    await served(api, w, queue, [600] * 10 + [9000] * 3 + [20] * 2, YESTERDAY_11)
    _, second = await two_joined(api, w)
    # one 600 s token ahead: 10 min, no spread -> 10 to 12 (minimum width)
    assert second["eta"] == {"min_minutes": 10, "max_minutes": 12}
    assert (await token_row(api, str(second["id"]))).predicted_wait_sec == 600


async def test_todays_sessions_blend_in_from_the_third(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    await served(api, w, queue, [600] * 10, YESTERDAY_11)
    await served(api, w, queue, [1200] * 3, TODAY_10)  # today, hour 10: not in band 11
    _, second = await two_joined(api, w)
    # a walk-in uses the business's last sessions of any service: 600 x 10 and 1200 x 3.
    # Trimmed (1 off each end of 13): 600 x 9 and 1200 x 2 -> 7800 / 11 = 709.09 s.
    # Quartiles of the 13 (positions 3 and 9 of 0..12): 600 and 600: 109.09 below, 0 above.
    # Blend: 0.7 x 709.09 + 0.3 x 1200 = 856.36 s.
    # Range: 747.27 s (12.45 min -> 10) to 856.36 s (14.27 min -> 15).
    assert second["eta"] == {"min_minutes": 10, "max_minutes": 15}
    assert (await token_row(api, str(second["id"]))).predicted_wait_sec == 856


async def test_positions_follow_the_walk_in_ratio_like_call_next(api: Api) -> None:
    w = await world(api)
    queue = await ratio_queue(w, "AAAW")  # tokens 100..103, queued in that order
    async with api.sessions() as session:
        found = await queue_service.queue_positions(session, uuid.UUID(queue), CLOCK)
    by_number = {place.token_no: place for place in found.values()}
    # call-next serves A100, A101, then the walk-in's turn (W103), then A102
    assert [by_number[no].ahead_count for no in (100, 101, 103, 102)] == [0, 1, 2, 3]
    # every token 1800 s -/+ 360. W103: 3600 -/+ 509.1 s -> 51.5 to 68.5 min -> 50 to 70.
    assert by_number[103].eta == Eta(50, 70, 3600)
    # A102: 5400 -/+ 623.5 s -> 79.6 to 100.4 min -> 70 to 110 (steps of 10 over 60)
    assert by_number[102].eta == Eta(70, 110, 5400)
    tokens = (await snapshot(api, w.owner, queue)).json()["tokens"]
    places = {token["token_no"]: token["order_position"] for token in tokens}
    assert [places[no] for no in (100, 101, 103, 102)] == [1, 2, 3, 4]  # the staff view agrees


async def test_the_eta_read_is_a_fixed_number_of_queries(api: Api) -> None:
    w = await world(api)
    small = await ratio_queue(w, "AW")
    other = await world(api)
    large = await ratio_queue(other, "AW" * 20)
    counts = []
    for queue in (small, large):
        async with api.sessions() as session:
            with statements(api) as seen:
                await queue_service.queue_positions(session, uuid.UUID(queue), CLOCK)
            counts.append(len(seen))
    assert counts == [5, 5]


async def cached(
    api: Api, queue: str, redis: Redis | None = None
) -> tuple[int, dict[uuid.UUID, queue_service.Position]]:
    """Statements used and the positions, through the cache."""
    async with api.sessions() as session:
        with statements(api) as seen:
            found = await queue_service.cached_positions(
                session, redis or api.app.state.redis, uuid.UUID(queue), CLOCK, 5
            )
    return len(seen), found


async def test_an_empty_queue_is_cached_under_event_zero(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    assert await cached(api, queue) == (6, {})  # newest event id, then the 5 reads
    assert await cached(api, queue) == (1, {})
    assert await api.app.state.redis.exists(f"eta:{queue}:0") == 1


async def test_every_queue_change_is_recomputed_never_served_stale(api: Api) -> None:
    """Each action writes an event, so the next read misses the cache and matches the
    database."""
    w = await world(api)
    queue = await queue_id(w)
    _, a = await customer(api)
    _, b = await customer(api)
    _, c = await customer(api)
    a_id = (await join(api, a, queue)).json()["id"]
    b_id = (await join(api, b, queue)).json()["id"]
    c_id = (await join(api, c, queue)).json()["id"]

    async def then_fresh(kind: str) -> None:
        async with api.sessions() as session:
            newest = await session.scalar(
                select(OutboxEvent.event_type)
                .where(OutboxEvent.queue_id == uuid.UUID(queue))
                .order_by(OutboxEvent.id.desc())
                .limit(1)
            )
            truth = await queue_service.queue_positions(session, uuid.UUID(queue), CLOCK)
        assert newest == kind
        used, found = await cached(api, queue)
        assert (used, found) == (6, truth), kind
        assert (await cached(api, queue))[0] == 1  # and cached from here on

    await then_fresh("token.joined")
    raised = await priority(api, w.owner, c_id, 1, 1)
    assert raised.status_code == 200
    await then_fresh("token.priority_changed")
    first = (await call_next(api, w.owner, queue)).json()
    await then_fresh("token.called")
    started = await act(
        api, w.owner, first["id"], "start", first["version"], service_id=w.service_id
    )
    await then_fresh("token.started")
    await act(api, w.owner, first["id"], "complete", started.json()["version"])
    await then_fresh("token.completed")
    second = (await call_next(api, w.owner, queue)).json()
    await act(api, w.owner, second["id"], "skip", second["version"])
    await then_fresh("token.skipped")
    third = (await call_next(api, w.owner, queue)).json()
    await act(api, w.owner, third["id"], "no-show", third["version"])
    await then_fresh("token.no_show")
    remaining = {a_id, b_id, c_id} - {first["id"], third["id"]}
    leaver = {a_id: a, b_id: b, c_id: c}
    token = remaining.pop()
    assert (await leave(api, leaver[token], token)).status_code == 204
    await then_fresh("token.cancelled")
    assert (await set_status(api, w.owner, queue, "paused")).status_code == 200
    await then_fresh("queue.status_changed")


async def test_redis_down_computes_from_the_database(api: Api) -> None:
    w = await world(api)
    queue = await ratio_queue(w, "AW")
    dead = Redis.from_url("redis://127.0.0.1:1/0")  # nothing listens there
    try:
        used, found = await cached(api, queue, dead)
    finally:
        await dead.aclose()
    async with api.sessions() as session:
        truth = await queue_service.queue_positions(session, uuid.UUID(queue), CLOCK)
    assert (used, found) == (6, truth) and len(truth) == 2


async def test_base_key_matches_base_order(api: Api) -> None:
    """A token being added is placed with base_key(); stored ones come back in base_order()."""
    w = await world(api)
    queue = await queue_id(w)
    same = CLOCK - timedelta(minutes=5)
    specs = [(0, same), (2, CLOCK), (0, same), (1, same - timedelta(minutes=1)), (0, CLOCK)]
    for number, (level, queued) in enumerate(specs, 200):
        await add_token(api, w, queue, number, "waiting", level, queued)
    async with api.sessions() as session:
        stored = (
            await session.scalars(
                select(QueueToken)
                .where(QueueToken.queue_id == uuid.UUID(queue))
                .order_by(*queue_service.base_order())
            )
        ).all()
    assert [token.token_no for token in stored] == [
        token.token_no for token in sorted(stored, key=queue_service.base_key)
    ]
    assert [token.token_no for token in stored] == [201, 203, 200, 202, 204]


async def test_the_hour_band_is_the_local_hour_on_a_dst_night(api: Api) -> None:
    """New York falls back on 1 November 2026: 01:30 happens at 05:30 UTC (EDT) and again at
    06:30 UTC (EST). Both are local hour 1, so both are in the band at 01:40 EST."""
    now = datetime.fromisoformat("2026-11-01T06:40:00+00:00")
    w = await world(api, zone="America/New_York", clock=now)
    queue = await queue_id(w)
    edt, est = datetime.fromisoformat("2026-11-01T05:30:00+00:00"), now - timedelta(minutes=10)
    await served(api, w, queue, [600] * 5, edt)
    await served(api, w, queue, [600] * 5, est)
    await served(api, w, queue, [1200] * 10, now - timedelta(days=1, hours=5))  # another hour
    async with api.sessions() as session:
        data = await queue_service.gather(session, uuid.UUID(queue), now)
    assert data is not None
    # band (10 x 600) wins: 600, blended with today's 10 x 600: still 600. Were the band the
    # UTC hour, only 5 would match; tier 2 (all 20) gives 900 and the blend 810.
    assert data.by_service[uuid.UUID(w.service_id)].mean == 600


async def eta_of(api: Api, headers: dict[str, str], token: str) -> object:
    return (await api.client.get(f"/v1/tokens/{token}", headers=headers)).json()["eta"]


async def test_first_in_line_waits_for_the_session_in_progress_and_the_called_token(
    api: Api,
) -> None:
    w = await world(api, service={"duration_min": 20})  # no samples: 1200 s -/+ 240 s
    queue = await queue_id(w)
    people = [await customer(api) for _ in range(3)]
    tokens = []
    for _, headers in people:
        tokens.append((await join(api, headers, queue)).json()["id"])
        api.clock.now += 1
    first = (await call_next(api, w.owner, queue)).json()
    started = await act(
        api, w.owner, first["id"], "start", first["version"], service_id=w.service_id
    )
    assert started.status_code == 200, started.text
    api.clock.now += 5 * 60
    # second in line has nothing waiting ahead, but 1200 - 300 = 900 s left on the session in
    # progress (a point value, no spread): 15 min -> 15 to 17 (minimum width), not 0 to 2
    assert await eta_of(api, people[1][1], tokens[1]) == {"min_minutes": 15, "max_minutes": 17}
    second = (await call_next(api, w.owner, queue)).json()
    assert second["id"] == tokens[1]
    # third: 900 s left plus the called (not started) token, 1200 -/+ 240 s:
    # 2100 -/+ 240 = 1860 to 2340 s = 31 to 39 min -> 30 to 40
    third = (await api.client.get(f"/v1/tokens/{tokens[2]}", headers=people[2][1])).json()
    assert (third["ahead_count"], third["eta"]) == (0, {"min_minutes": 30, "max_minutes": 40})


async def test_sessions_older_than_the_lookback_leave_the_hour_band(api: Api) -> None:
    """Tier 1 (this service in this local hour) only searches the last 90 days."""
    for days, expected_mean in ((91, 900.0), (89, 600.0)):
        w = await world(api)
        queue = await queue_id(w)
        await served(api, w, queue, [600] * 10, YESTERDAY_11 - timedelta(days=days - 1))
        await served(api, w, queue, [1200] * 10, TODAY_10 - timedelta(days=1))  # hour 10
        async with api.sessions() as session:
            data = await queue_service.gather(session, uuid.UUID(queue), CLOCK)
        assert data is not None
        # 89 days: the band holds the 10 x 600 -> 600. 91 days: the band is empty, so tier 2
        # (the last 50 of the service: all 20) -> trimmed 2 off each end: 600 x 8 and
        # 1200 x 8 -> 900.
        assert data.by_service[uuid.UUID(w.service_id)].mean == expected_mean, days


async def test_cached_etas_never_cross_queues_or_businesses(api: Api) -> None:
    short, long = await world(api), await world(api, service={"duration_min": 60})
    queues = {}
    for w in (short, long):
        queue = await queue_id(w)
        for _ in range(2):  # tokens 1 and 2 in both queues
            _, headers = await customer(api)
            assert (await join(api, headers, queue)).status_code == 201
            api.clock.now += 1
        queues[w.service_id] = queue
    seen = {}
    for w in (short, long):
        _, found = await cached(api, queues[w.service_id])
        seen[w.service_id] = {place.token_no: place.eta for place in found.values()}
    # token 2 waits behind one walk-in: 1800 -/+ 360 s -> 20 to 40; 3600 -/+ 720 s -> 45 to 80
    assert seen[short.service_id][2] == Eta(20, 40, 1800)
    assert seen[long.service_id][2] == Eta(45, 80, 3600)
    for w in (short, long):  # read again: each queue gets its own entry back
        used, found = await cached(api, queues[w.service_id])
        assert used == 1
        assert {place.token_no: place.eta for place in found.values()} == seen[w.service_id]
    keys = sorted(key.decode() for key in await api.app.state.redis.keys("eta:*"))
    assert all(any(queue in key for key in keys) for queue in queues.values())


async def test_the_eta_moves_when_a_token_ahead_skips_leaves_or_is_closed_out(
    api: Api,
) -> None:
    w = await world(api)
    queue = await queue_id(w)
    people = [await customer(api) for _ in range(4)]
    tokens = []
    for _, headers in people:
        tokens.append((await join(api, headers, queue)).json()["id"])
        api.clock.now += 1
    last = people[3][1]
    # 3 tokens of 1800 -/+ 360 s ahead: 5400 -/+ 623.5 -> 79.6 to 100.4 min -> 70 to 110
    assert await eta_of(api, last, tokens[3]) == {"min_minutes": 70, "max_minutes": 110}
    called = (await call_next(api, w.owner, queue)).json()
    skipped = await act(api, w.owner, called["id"], "skip", called["version"])
    assert skipped.status_code == 200, skipped.text
    # the skipped token goes behind: 2 ahead, 3600 -/+ 509.1 -> 51.5 to 68.5 min -> 50 to 70
    assert await eta_of(api, last, tokens[3]) == {"min_minutes": 50, "max_minutes": 70}
    assert (await leave(api, people[1][1], tokens[1])).status_code == 204
    # 1 ahead: 1800 -/+ 360 -> 24 to 36 min -> 20 to 40
    assert await eta_of(api, last, tokens[3]) == {"min_minutes": 20, "max_minutes": 40}
    midnight = datetime.fromisoformat("2026-11-02T00:00:00+05:30")
    await queue_jobs.end_of_day(api.sessions, midnight + timedelta(minutes=1))
    after = (await api.client.get(f"/v1/tokens/{tokens[3]}", headers=last)).json()
    assert (after["status"], after["eta"]) == ("cancelled", None)  # closed out with the rest
