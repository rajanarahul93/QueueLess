"""Worker jobs: no-show sweeper, promotion, end of day, and the walk-in ratio in call-next.

Each test gets a fresh database: the jobs scan every business, so rows left by other tests
would change their counts.
"""

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from sqlalchemy import func, insert, select, text, update

from app.core.security import create_access_token
from app.models import AuditLog, Queue, QueueToken
from app.realtime.outbox import queue_lock_key
from app.services import queue_jobs
from tests.integration.api import Api
from tests.integration.db import alembic_config, reset_schema
from tests.integration.test_availability import utc
from tests.integration.test_booking import World, book_one, customer, world
from tests.integration.test_queues import add_token, queue_id, snapshot
from tests.integration.test_serving import (
    act,
    appointment_status,
    appointment_token,
    call_next,
    no_shows,
    priority,
)
from tests.integration.test_tokens import check_in, join, set_status, settings, token_row

pytestmark = pytest.mark.anyio

JOB_ACTIONS = {"token.mark_no_show", "token.promote", "token.end_of_day", "queue.close"}


@pytest.fixture
def migrated_url(postgres_url: str) -> str:
    """A fresh schema for every test in this module."""
    reset_schema(postgres_url)
    command.upgrade(alembic_config(postgres_url), "head")
    return postgres_url


async def audit_rows(api: Api, entity: str) -> list[AuditLog]:
    async with api.sessions() as session:
        rows = await session.scalars(
            select(AuditLog).where(AuditLog.entity_id == uuid.UUID(entity)).order_by(AuditLog.id)
        )
        return list(rows.all())


# --- no-show sweeper ---


async def test_sweeper_boundary_d29_effects_audit_and_rerun(api: Api) -> None:
    w = await world(api)
    user, appointment_id, token = await appointment_token(w)  # clock at the 09:00 slot
    queue = str((await token_row(api, token)).queue_id)
    assert (await call_next(api, w.owner, queue)).json()["id"] == token
    called = (await token_row(api, token)).called_at
    assert called is not None
    early = await queue_jobs.sweep_no_shows(api.sessions, called + timedelta(seconds=299))
    assert (early.processed, early.changed) == (0, 0)
    due = await queue_jobs.sweep_no_shows(api.sessions, called + timedelta(minutes=5))
    assert (due.processed, due.changed, due.skipped, due.failed) == (1, 1, 0, 0)
    row = await token_row(api, token)
    assert (row.status, row.version) == ("no_show", 3)
    assert await no_shows(api, user.id) == 1
    assert await appointment_status(api, appointment_id) == "no_show"
    swept = [r for r in await audit_rows(api, token) if r.action == "token.mark_no_show"]
    assert len(swept) == 1 and swept[0].actor_id is None
    assert swept[0].data["reason"] == "grace_expired"
    again = await queue_jobs.sweep_no_shows(api.sessions, called + timedelta(minutes=9))
    assert (again.processed, again.changed) == (0, 0)
    assert await no_shows(api, user.id) == 1


async def test_sweeper_uses_each_business_grace(api: Api) -> None:
    quick, slow = await world(api), await world(api)
    await settings(quick, grace_period_min=1)
    await settings(slow, grace_period_min=10)
    moment = utc(9)
    fast_token = await add_token(api, quick, await queue_id(quick), 1, "called", called_at=moment)
    slow_token = await add_token(api, slow, await queue_id(slow), 1, "called", called_at=moment)
    summary = await queue_jobs.sweep_no_shows(api.sessions, moment + timedelta(minutes=2))
    assert summary.changed == 1
    assert (await token_row(api, fast_token)).status == "no_show"
    assert (await token_row(api, slow_token)).status == "called"


async def test_sweeper_batches_drain_a_backlog(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    for no in range(1, 6):
        await add_token(api, w, queue, no, "called", called_at=utc(9) + timedelta(seconds=no))
    later = utc(10)
    runs = [
        (await queue_jobs.sweep_no_shows(api.sessions, later, batch=2)).changed for _ in range(4)
    ]
    assert runs == [2, 2, 1, 0]


async def test_a_poison_row_does_not_block_the_others(
    api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    w = await world(api)
    queue = await queue_id(w)
    tokens = [
        await add_token(api, w, queue, no, "called", called_at=utc(9) + timedelta(seconds=no))
        for no in range(1, 4)
    ]
    poison = tokens[1]
    async with api.sessions() as session:
        await session.execute(
            text(
                "CREATE FUNCTION poison() RETURNS trigger AS $$ BEGIN "
                f"IF NEW.id = '{poison}' THEN RAISE EXCEPTION 'poison row'; END IF; "
                "RETURN NEW; END $$ LANGUAGE plpgsql"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER poison BEFORE UPDATE ON queue_tokens "
                "FOR EACH ROW EXECUTE FUNCTION poison()"
            )
        )
        await session.commit()
    with caplog.at_level(logging.INFO, logger="app.services.queue_jobs"):
        summary = await queue_jobs.sweep_no_shows(api.sessions, utc(10))
    assert (summary.processed, summary.changed, summary.failed) == (3, 2, 1)
    assert [(await token_row(api, t)).status for t in tokens] == ["no_show", "called", "no_show"]
    failure = [r.getMessage() for r in caplog.records if "failed token_id" in r.getMessage()]
    assert failure == [f"no_show_sweeper failed token_id={poison} error=DBAPIError"]
    assert any("'failed': 1" in r.getMessage() for r in caplog.records)  # the summary line


# --- promotion ---


async def test_promotion_boundary_and_what_it_leaves_alone(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    joined = utc(9)
    waiting = await add_token(api, w, queue, 1, "waiting", 0, joined, joined_at=joined)
    raised = await add_token(api, w, queue, 2, "waiting", 2, joined, joined_at=joined)
    medium = await add_token(api, w, queue, 3, "waiting", 1, joined, joined_at=joined)
    called = await add_token(api, w, queue, 4, "called", 0, joined, joined_at=joined)
    # A skip moved queued_at later, but the wait counts from joined_at.
    skipped = await add_token(
        api, w, queue, 5, "waiting", 0, joined + timedelta(minutes=15), joined_at=joined
    )
    limit = joined + timedelta(minutes=20)
    assert (await queue_jobs.promote_waiting(api.sessions, limit)).changed == 0  # not "exceeds"
    summary = await queue_jobs.promote_waiting(api.sessions, limit + timedelta(seconds=1))
    assert summary.changed == 2
    assert [(await token_row(api, t)).priority for t in (waiting, skipped)] == [1, 1]
    assert (await token_row(api, waiting)).version == 2
    assert [(await token_row(api, t)).priority for t in (raised, medium, called)] == [2, 1, 0]
    (promote,) = [r for r in await audit_rows(api, waiting) if r.action == "token.promote"]
    assert promote.actor_id is None and promote.data == {"token_no": 1, "from": 0, "to": 1}


async def test_a_promoted_token_lowered_by_staff_is_not_promoted_again(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    joined = utc(9)
    token = await add_token(api, w, queue, 1, "waiting", 0, joined, joined_at=joined)
    assert (
        await queue_jobs.promote_waiting(api.sessions, joined + timedelta(minutes=21))
    ).changed == 1
    lowered = await priority(api, w.owner, token, 0, 2)
    assert lowered.status_code == 200 and lowered.json()["priority"] == 0
    later = await queue_jobs.promote_waiting(api.sessions, joined + timedelta(minutes=50))
    assert later.changed == 0 and (await token_row(api, token)).priority == 0


async def test_promotion_uses_each_business_limit(api: Api) -> None:
    eager, patient = await world(api), await world(api)
    await settings(eager, promotion_wait_limit_min=5)
    joined = utc(9)
    first = await add_token(
        api, eager, await queue_id(eager), 1, "waiting", 0, joined, joined_at=joined
    )
    second = await add_token(
        api, patient, await queue_id(patient), 1, "waiting", 0, joined, joined_at=joined
    )
    await queue_jobs.promote_waiting(api.sessions, joined + timedelta(minutes=6))
    assert [(await token_row(api, t)).priority for t in (first, second)] == [1, 0]


# --- end of day ---


async def past_queue(w: World, day: date) -> str:
    async with w.api.sessions() as session:
        created = await session.scalar(
            insert(Queue)
            .values(
                business_id=w.business_id, resource_id=w.resource, queue_date=day, status="open"
            )
            .returning(Queue.id)
        )
        await session.commit()
    return str(created)


async def queue_status(api: Api, queue: str) -> str:
    async with api.sessions() as session:
        return str(await session.scalar(select(Queue.status).where(Queue.id == uuid.UUID(queue))))


async def test_end_of_day_in_kolkata(api: Api) -> None:
    w = await world(api)  # Asia/Kolkata
    user, appointment_id, booked = await appointment_token(w)  # Monday 2 Nov queue
    queue = str((await token_row(api, booked)).queue_id)
    waiting = await add_token(api, w, queue, 50, "waiting")
    called = await add_token(api, w, queue, 51, "called", called_at=utc(9))
    serving = await add_token(api, w, queue, 52, "in_service")
    before = datetime(2026, 11, 2, 18, 29, tzinfo=UTC)  # 23:59 on 2 Nov in Kolkata
    assert (await queue_jobs.end_of_day(api.sessions, before)).changed == 0
    summary = await queue_jobs.end_of_day(api.sessions, before + timedelta(minutes=1))
    assert (summary.processed, summary.changed) == (3, 4)  # three tokens and the queue
    statuses = [(await token_row(api, t)).status for t in (booked, waiting, called, serving)]
    assert statuses == ["cancelled", "cancelled", "cancelled", "in_service"]
    assert await queue_status(api, queue) == "closed"
    assert await appointment_status(api, appointment_id) == "cancelled"
    assert await no_shows(api, user.id) == 0
    rows = await audit_rows(api, waiting)
    assert [(r.action, r.actor_id) for r in rows] == [("token.end_of_day", None)]
    (closed,) = await audit_rows(api, queue)
    assert (closed.action, closed.actor_id) == ("queue.close", None)
    again = await queue_jobs.end_of_day(api.sessions, before + timedelta(hours=3))
    assert (again.processed, again.changed) == (0, 0)
    # The in-service token can still be finished by staff after the close.
    assert (await act(api, w.owner, serving, "complete", 1)).status_code == 200


@pytest.mark.parametrize(
    ("zone", "day", "last_moment"),
    [
        # Behind UTC, on the DST change: 31 Oct ends at 00:00 EDT = 04:00 UTC on 1 Nov...
        ("America/New_York", date(2026, 10, 31), datetime(2026, 11, 1, 4, 0, tzinfo=UTC)),
        # ...and 1 Nov has 25 hours: it ends at 00:00 EST = 05:00 UTC on 2 Nov.
        ("America/New_York", date(2026, 11, 1), datetime(2026, 11, 2, 5, 0, tzinfo=UTC)),
        # Ahead of UTC.
        (
            "Asia/Kolkata",
            date(2026, 11, 1),
            datetime(2026, 10, 31, 18, 30, tzinfo=UTC) + timedelta(days=1),
        ),
        # UTC+14: 2 Nov ends at 10:00 UTC on 2 Nov.
        ("Pacific/Kiritimati", date(2026, 11, 2), datetime(2026, 11, 2, 10, 0, tzinfo=UTC)),
    ],
)
async def test_end_of_day_follows_the_business_zone(
    api: Api, zone: str, day: date, last_moment: datetime
) -> None:
    w = await world(api, zone=zone)
    queue = await past_queue(w, day)
    token = await add_token(api, w, queue, 1, "waiting")
    early = await queue_jobs.end_of_day(api.sessions, last_moment - timedelta(seconds=1))
    assert early.changed == 0 and (await token_row(api, token)).status == "waiting"
    await queue_jobs.end_of_day(api.sessions, last_moment)
    assert (await token_row(api, token)).status == "cancelled"
    assert await queue_status(api, queue) == "closed"


async def test_end_of_day_catches_up_after_an_outage(api: Api) -> None:
    w = await world(api)
    queues = [await past_queue(w, date(2026, 10, d)) for d in (27, 28, 29)]
    tokens = [await add_token(api, w, q, n, "waiting") for q in queues for n in (1, 2)]
    now = utc(9)  # Sunday 1 Nov, three days later
    runs = [await queue_jobs.end_of_day(api.sessions, now, batch=4) for _ in range(3)]
    assert [r.processed for r in runs] == [4, 2, 0]
    assert {(await token_row(api, t)).status for t in tokens} == {"cancelled"}
    assert [await queue_status(api, q) for q in queues] == ["closed"] * 3


# --- system actor ---


async def test_only_worker_jobs_write_audit_rows_without_an_actor(api: Api) -> None:
    w = await world(api)
    staff = await w.staff()
    _, _, checked_in = await appointment_token(w)
    queue = str((await token_row(api, checked_in)).queue_id)
    _, booker = await customer(api)
    other = await book_one(w, booker, 11)
    api.clock.now = (utc(11) - timedelta(minutes=10)).timestamp()
    assert (await check_in(api, staff, other)).status_code == 201  # staff check-in
    called = (await call_next(api, staff, queue)).json()
    await act(api, staff, called["id"], "skip", called["version"])
    again = (await call_next(api, staff, queue)).json()
    await act(api, staff, again["id"], "start", again["version"])
    await act(api, staff, again["id"], "complete", again["version"] + 1)
    _, walker = await customer(api)
    joined = (await join(api, walker, queue)).json()
    await priority(api, w.owner, joined["id"], 1, joined["version"])
    await set_status(api, w.owner, queue, "paused")
    _, canceller = await customer(api)
    cancelled = await book_one(w, canceller, 15)
    await api.client.delete(f"/v1/appointments/{cancelled}", headers=staff)  # staff cancel
    async with api.sessions() as session:
        nulls = await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.actor_id.is_(None))
        )
        total = await session.scalar(select(func.count()).select_from(AuditLog))
    assert nulls == 0 and (total or 0) >= 8
    later = datetime.fromtimestamp(api.clock.now, UTC) + timedelta(days=2)
    for job in (queue_jobs.sweep_no_shows, queue_jobs.promote_waiting, queue_jobs.end_of_day):
        await job(api.sessions, later)
    async with api.sessions() as session:
        actions = set(
            (
                await session.scalars(select(AuditLog.action).where(AuditLog.actor_id.is_(None)))
            ).all()
        )
    assert actions and actions <= JOB_ACTIONS


# --- walk-in ratio through call-next ---


async def ratio_queue(w: World, spec: str, walk_ins: int = 1, appointments: int = 2) -> str:
    await settings_ratio(w, walk_ins, appointments)
    queue = await queue_id(w)
    for no, kind in enumerate(spec, 100):  # clear of the numbers real joins take
        await add_token(
            w.api,
            w,
            queue,
            no,
            "waiting",
            2 if kind == "P" else 0,
            utc(8) + timedelta(minutes=no),
            source="appointment" if kind == "A" else "walk_in",
        )
    return queue


async def settings_ratio(w: World, walk_ins: int, appointments: int) -> None:
    response = await w.api.client.patch(
        f"/v1/businesses/{w.business_id}",
        json={"settings": {"walk_in_ratio": {"walk_ins": walk_ins, "appointments": appointments}}},
        headers=w.owner,
    )
    assert response.status_code == 200, response.text


async def call_sources(api: Api, w: World, queue: str, count: int) -> str:
    out = []
    for _ in range(count):
        body = (await call_next(api, w.owner, queue)).json()
        out.append(
            "P" if body["priority"] == 2 else ("A" if body["source"] == "appointment" else "W")
        )
    return "".join(out)


@pytest.mark.parametrize(
    ("spec", "ratio", "expected"),
    [
        ("AAAAWW", (1, 2), "AAWAAW"),
        ("AAAAAAWWWW", (2, 3), "AAAWWAAAWW"),
        ("AAAWWW", (1, 1), "AWAWAW"),
        ("AAAA", (1, 2), "AAAA"),  # no walk-in waiting
    ],
)
async def test_call_next_follows_the_ratio(
    api: Api, spec: str, ratio: tuple[int, int], expected: str
) -> None:
    w = await world(api)
    queue = await ratio_queue(w, spec, *ratio)
    positions = (await snapshot(api, w.owner, queue)).json()["tokens"]
    predicted = "".join(
        "A" if t["source"] == "appointment" else "W"
        for t in sorted(positions, key=lambda t: t["order_position"])
    )
    assert predicted == expected  # the snapshot shows the order call-next will follow
    assert await call_sources(api, w, queue, len(spec)) == expected


async def test_a_waiting_priority_two_token_comes_first_even_on_a_walk_in_turn(api: Api) -> None:
    w = await world(api)
    queue = await ratio_queue(w, "AAWA")
    assert await call_sources(api, w, queue, 2) == "AA"  # now it is a walk-in's turn
    async with api.sessions() as session:
        await session.execute(
            update(QueueToken)
            .where(QueueToken.queue_id == uuid.UUID(queue), QueueToken.token_no == 103)
            .values(priority=2)
        )
        await session.commit()
    assert await call_sources(api, w, queue, 2) == "PW"


async def test_ratio_after_a_skip_and_after_a_leave(api: Api) -> None:
    w = await world(api)
    queue = await ratio_queue(w, "AAAWW")
    await call_next(api, w.owner, queue)
    second = (await call_next(api, w.owner, queue)).json()
    await act(api, w.owner, second["id"], "skip", second["version"])  # drops out of history
    assert await call_sources(api, w, queue, 1) == "A"  # history is A (first) only: no turn yet
    walk_in = await token_row_by_no(api, queue, 103)
    walker = {"Authorization": f"Bearer {_token(api, walk_in.customer_id)}"}
    await api.client.delete(f"/v1/tokens/{walk_in.id}", headers=walker)  # the oldest walk-in leaves
    assert await call_sources(api, w, queue, 1) == "W"  # the next walk-in takes the turn


async def token_row_by_no(api: Api, queue: str, number: int) -> QueueToken:
    async with api.sessions() as session:
        found = await session.scalar(
            select(QueueToken).where(
                QueueToken.queue_id == uuid.UUID(queue), QueueToken.token_no == number
            )
        )
        assert found is not None
        return found


def _token(api: Api, user_id: uuid.UUID) -> str:
    return create_access_token(api.settings, user_id, "customer")


@pytest.mark.parametrize("status", ["paused", "closed"])
async def test_ratio_applies_in_paused_and_closed_queues(api: Api, status: str) -> None:
    w = await world(api)
    queue = await ratio_queue(w, "AAAW")
    await set_status(api, w.owner, queue, status)
    assert await call_sources(api, w, queue, 4) == "AAWA"


# --- the advisory lock blocks only call-next on the same queue ---


async def until_waits(api: Api, count: int) -> None:
    """Until `count` locks are waiting to be granted."""
    async with api.sessions() as probe:
        for _ in range(20000):
            waits = await probe.scalar(text("SELECT count(*) FROM pg_locks WHERE NOT granted"))
            await probe.rollback()
            if (waits or 0) >= count:
                return
            await asyncio.sleep(0)
    pytest.fail(f"never saw {count} waiting lock(s)")


async def test_the_queue_event_lock_only_holds_back_that_queue(api: Api) -> None:
    """Since the outbox, every writer of a queue takes its lock just before commit (events of a
    queue commit in id order). While it is held, other queues carry on; this queue's call-next,
    join and status change wait, then complete."""
    w = await world(api)
    other = await world(api)
    queue = await ratio_queue(w, "AW")
    elsewhere = await ratio_queue(other, "A")
    async with api.sessions() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": queue_lock_key(uuid.UUID(queue))}
        )
        assert (await call_next(api, other.owner, elsewhere)).status_code == 200  # other queue
        _, walker = await customer(api)
        waiting = [
            asyncio.create_task(call_next(api, w.owner, queue)),
            asyncio.create_task(join(api, walker, queue)),
        ]
        await until_waits(api, 2)
        # Started only once the join holds the queue row, so the pause commits after the join
        # (a pause first would rightly turn the join into a 409).
        waiting.append(asyncio.create_task(set_status(api, w.owner, queue, "paused")))
        await until_waits(api, 3)  # the status change waits on the row the join holds
        assert not any(task.done() for task in waiting)
        await holder.commit()  # releases the lock
    assert [(await task).status_code for task in waiting] == [200, 201, 200]


# --- the real arq path ---


async def test_each_job_through_the_worker_functions(
    api: Api, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REDIS_URL", redis_url)
    from app.workers import main as worker

    assert not hasattr(worker, "noop")
    jobs = {job.name: job for job in worker.WorkerSettings.cron_jobs}
    assert set(jobs) == {
        "cron:sweep_no_shows",
        "cron:promote_waiting",
        "cron:end_of_day",
        "cron:clean_retention",
    }
    assert jobs["cron:clean_retention"].minute == set(range(0, 60, 10))
    for job in jobs.values():
        assert (job.timeout_s, job.max_tries, job.unique) == (50, 1, True)
    w = await world(api)
    queue = await queue_id(w)  # Sunday 1 Nov in Kolkata
    morning, afternoon = utc(9, day=1), utc(16, day=1)
    swept = await add_token(api, w, queue, 1, "called", called_at=morning, joined_at=morning)
    promoted = await add_token(api, w, queue, 2, "waiting", 0, morning, joined_at=morning)
    past = await past_queue(w, date(2026, 10, 30))
    leftover = await add_token(api, w, past, 1, "waiting", 0, afternoon, joined_at=afternoon)
    ctx: dict[str, Any] = {"settings": api.settings, "clock": lambda: afternoon.timestamp()}
    await worker.startup(ctx)
    try:
        results = [
            await worker.sweep_no_shows(ctx),
            await worker.promote_waiting(ctx),
            await worker.end_of_day(ctx),
        ]
    finally:
        await worker.shutdown(ctx)
    assert [r["changed"] for r in results] == [1, 1, 2]  # end of day: the token and its queue
    assert set(results[0]) == {"processed", "changed", "skipped", "failed", "duration_ms"}
    assert (await token_row(api, swept)).status == "no_show"
    assert (await token_row(api, promoted)).priority == 1
    assert (await token_row(api, leftover)).status == "cancelled"


async def test_a_token_called_again_while_the_sweeper_runs_is_not_swept(api: Api) -> None:
    """Selected as due, then skipped and called again (still 'called', but fresh) before the
    sweeper's update: only the version condition stops it being marked a no-show."""
    w = await world(api)
    queue = await queue_id(w)
    token = await add_token(api, w, queue, 1, "called", called_at=utc(9, day=1))
    fresh = utc(10, day=1)
    async with api.sessions() as holder:  # the skip and the new call, not yet committed
        await holder.execute(
            update(QueueToken)
            .where(QueueToken.id == uuid.UUID(token))
            .values(version=QueueToken.version + 2, called_at=fresh)
        )
        sweep = asyncio.create_task(queue_jobs.sweep_no_shows(api.sessions, fresh))
        async with api.sessions() as probe:
            for _ in range(20000):
                waits = await probe.scalar(text("SELECT count(*) FROM pg_locks WHERE NOT granted"))
                await probe.rollback()
                if waits:
                    break
                await asyncio.sleep(0)
            else:
                pytest.fail("the sweeper never waited on the token row")
        await holder.commit()
    summary = await sweep
    assert (summary.changed, summary.skipped) == (0, 1)
    row = await token_row(api, token)
    assert (row.status, row.version) == ("called", 3)
