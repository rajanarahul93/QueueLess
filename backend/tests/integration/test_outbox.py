"""Outbox events: one per change, in the same transaction, ordered per queue, relayed in order.

Each test gets a fresh database, so event counts belong to the test alone.
"""

import ast
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, text

from app.models import OutboxEvent, QueueToken
from app.realtime.outbox import MAX_PAYLOAD_BYTES, emit_event
from app.realtime.relay import relay_pass
from app.services import queue_jobs
from tests.integration.api import Api
from tests.integration.db import alembic_config, reset_schema
from tests.integration.test_availability import utc
from tests.integration.test_booking import book_one, customer, local, world
from tests.integration.test_queues import add_token, queue_id
from tests.integration.test_serving import act, appointment_token, call_next, priority
from tests.integration.test_tokens import join, leave, set_status, token_row

pytestmark = pytest.mark.anyio


@pytest.fixture
def migrated_url(postgres_url: str) -> str:
    reset_schema(postgres_url)
    command.upgrade(alembic_config(postgres_url), "head")
    return postgres_url


async def events(api: Api) -> list[OutboxEvent]:
    async with api.sessions() as session:
        return list((await session.scalars(select(OutboxEvent).order_by(OutboxEvent.id))).all())


async def until_waiting(api: Api, count: int) -> None:
    async with api.sessions() as probe:
        for _ in range(20000):
            waits = await probe.scalar(text("SELECT count(*) FROM pg_locks WHERE NOT granted"))
            await probe.rollback()
            if (waits or 0) >= count:
                return
            await asyncio.sleep(0)
    pytest.fail(f"never saw {count} waiting lock(s)")


async def messages(pubsub: Any, count: int) -> list[dict[str, Any]]:
    """The next `count` published messages; subscribe confirmations are skipped."""
    received: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + 10
    while len(received) < count:
        assert asyncio.get_running_loop().time() < deadline, received
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
        if message is not None:
            received.append(json.loads(message["data"]))
    return received


def probe_event(queue: uuid.UUID, name: str) -> dict[str, Any]:
    return {
        "business_id": uuid.uuid4(),
        "queue_id": queue,
        "event_type": name,
        "payload": {"probe": name},
        "at": datetime.now(UTC),
    }


# --- ordering ---


async def test_events_of_one_queue_commit_in_id_order(api: Api) -> None:
    """T1 draws an id and stays open; T2 on the same queue cannot draw one until T1 commits."""
    queue = uuid.uuid4()
    async with api.sessions() as first:
        await emit_event(first, **probe_event(queue, "t1"))
        async with api.sessions() as second:
            late = asyncio.create_task(emit_event(second, **probe_event(queue, "t2")))
            await until_waiting(api, 1)
            assert not late.done()
            await first.commit()
            await late
            await second.commit()
    rows = [(e.event_type, e.id) for e in await events(api)]
    assert [name for name, _ in rows] == ["t1", "t2"] and rows[0][1] < rows[1][1]


async def test_a_lower_id_of_another_queue_that_commits_late_is_still_relayed(
    api: Api, redis_url: str
) -> None:
    queue_a, queue_b = uuid.uuid4(), uuid.uuid4()
    redis = Redis.from_url(redis_url)
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(f"queue:{queue_a}", f"queue:{queue_b}")
        async with api.sessions() as slow:
            await emit_event(slow, **probe_event(queue_a, "a"))  # the lower id, still open
            async with api.sessions() as fast:
                await emit_event(fast, **probe_event(queue_b, "b"))  # a higher id
                await fast.commit()
            assert await relay_pass(api.sessions, redis) == 1  # only b is visible
            await slow.commit()
        assert await relay_pass(api.sessions, redis) == 1  # a is not skipped
        assert await relay_pass(api.sessions, redis) == 0
        received = await messages(pubsub, 2)
        assert [m["type"] for m in received] == ["b", "a"]
        assert received[1]["id"] < received[0]["id"]  # a has the lower id, published later
        async with api.sessions() as session:
            replay = await session.scalars(
                select(OutboxEvent.event_type).where(
                    OutboxEvent.queue_id == queue_a, OutboxEvent.id > 0
                )
            )
            assert list(replay.all()) == ["a"]
            unpublished = await session.scalar(
                select(OutboxEvent.id).where(OutboxEvent.published_at.is_(None))
            )
            assert unpublished is None
    finally:
        await pubsub.aclose()
        await redis.aclose()


async def test_relay_publishes_a_queue_in_id_order_and_marks_only_after_publishing(
    api: Api, redis_url: str
) -> None:
    queue = uuid.uuid4()
    async with api.sessions() as session:
        for name in ("e1", "e2", "e3"):
            await emit_event(session, **probe_event(queue, name))
        await session.commit()
    dead = Redis.from_url("redis://127.0.0.1:1/0")  # nothing listens there
    with pytest.raises(RedisError):
        await relay_pass(api.sessions, dead)
    await dead.aclose()
    assert all(e.published_at is None for e in await events(api))  # still unpublished
    redis = Redis.from_url(redis_url)
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(f"queue:{queue}")
        assert await relay_pass(api.sessions, redis, batch=2) == 2
        assert await relay_pass(api.sessions, redis, batch=2) == 1
        names = [m["type"] for m in await messages(pubsub, 3)]
        assert names == ["e1", "e2", "e3"]
        assert all(e.published_at is not None for e in await events(api))
    finally:
        await pubsub.aclose()
        await redis.aclose()


# --- one event per change, in the same transaction ---


async def test_a_rolled_back_change_writes_no_event(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    _, headers = await customer(api)
    assert (await join(api, headers, queue)).status_code == 201
    again = await join(api, headers, queue)  # fails after drawing a number: rolled back
    assert again.status_code == 409
    async with api.sessions() as session:
        await emit_event(session, **probe_event(uuid.uuid4(), "never"))
        await session.rollback()
    assert [e.event_type for e in await events(api)] == ["token.joined"]


async def test_a_change_whose_event_fails_does_not_land(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The event is written in the change's own transaction: no event, no change."""
    w = await world(api)
    queue = await queue_id(w)
    _, headers = await customer(api)

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("outbox write failed")

    monkeypatch.setattr("app.services.queue.emit_token_event", broken)
    with pytest.raises(RuntimeError, match="outbox write failed"):
        await join(api, headers, queue)
    async with api.sessions() as session:
        assert await session.scalar(select(QueueToken.id)) is None


async def test_payloads_are_capped(api: Api) -> None:
    async with api.sessions() as session:
        big = probe_event(uuid.uuid4(), "big") | {"payload": {"x": "y" * MAX_PAYLOAD_BYTES}}
        with pytest.raises(ValueError, match="over 4096 bytes"):
            await emit_event(session, **big)


async def test_every_mutation_writes_its_event_and_nothing_personal(api: Api) -> None:
    w = await world(api)
    staff = await w.staff()
    user, _, checked_in = await appointment_token(w)  # Monday's queue, at 09:00
    queue = str((await token_row(api, checked_in)).queue_id)
    _, walker = await customer(api)
    walked = (await join(api, walker, queue)).json()
    await priority(api, staff, walked["id"], 1, walked["version"])
    called = (await call_next(api, staff, queue)).json()  # priority 1 first: the walk-in
    await act(api, staff, called["id"], "skip", called["version"])
    again = (await call_next(api, staff, queue)).json()
    await act(api, staff, again["id"], "start", again["version"], service_id=w.service_id)
    await act(api, staff, again["id"], "complete", again["version"] + 1)
    last = (await call_next(api, staff, queue)).json()
    await act(api, staff, last["id"], "no-show", last["version"])
    _, leaver = await customer(api)
    left = (await join(api, leaver, queue)).json()
    await leave(api, leaver, left["id"])
    await set_status(api, w.owner, queue, "paused")
    _, booker = await customer(api)
    booked = await book_one(w, booker, 13)
    await api.client.patch(
        f"/v1/appointments/{booked}/reschedule", json={"start": local(14)}, headers=booker
    )
    other = await book_one(w, booker, 15)
    await api.client.delete(f"/v1/appointments/{other}", headers=booker)
    # The three jobs.
    await add_token(api, w, queue, 900, "called", called_at=utc(8))  # due for the sweeper
    await add_token(api, w, queue, 901, "waiting", 0, utc(8), joined_at=utc(8))  # for promotion
    later = utc(9) + timedelta(hours=1)
    await queue_jobs.sweep_no_shows(api.sessions, later)
    await queue_jobs.promote_waiting(api.sessions, later)
    await queue_jobs.end_of_day(api.sessions, utc(9) + timedelta(days=1))
    rows = await events(api)
    by_type = [(e.event_type, e.payload.get("reason")) for e in rows]
    assert by_type == [
        ("appointment.booked", None),  # the booking behind the check-in
        ("token.joined", None),  # check-in
        ("token.joined", None),  # walk-in
        ("token.priority_changed", None),
        ("token.called", None),
        ("token.skipped", None),
        ("token.called", None),
        ("token.started", None),
        ("token.completed", None),
        ("token.called", None),
        ("token.no_show", None),
        ("token.joined", None),
        ("token.cancelled", "left"),
        ("queue.status_changed", None),
        ("appointment.booked", None),
        ("appointment.rescheduled", None),
        ("appointment.booked", None),
        ("appointment.cancelled", None),
        ("token.no_show", "grace_expired"),
        ("token.priority_changed", "promotion"),
        ("token.cancelled", "end_of_day"),
        ("queue.status_changed", "end_of_day"),
    ], by_type
    raw = json.dumps([e.payload for e in rows])
    assert str(user.id) not in raw  # no customer ids
    for forbidden in ("customer_id", "name", "phone", "email", "+91", "@"):
        assert forbidden not in raw, forbidden
    queue_ids = {str(e.queue_id) for e in rows if e.event_type.startswith(("token.", "queue."))}
    assert queue_ids == {queue}
    assert {e.queue_id for e in rows if e.event_type.startswith("appointment.")} == {None}


# --- no state change can forget its event ---

SERVICES = Path(__file__).resolve().parents[2] / "app" / "services"
# Commits that are not queue or booking state changes (accounts, business setup, lazy queue
# creation), reviewed when this test was written. A new committing function must emit or be
# added here on purpose.
ALLOWED_WITHOUT_EVENT = {
    "auth.py": {
        "issue_refresh_token",
        "rotate_refresh_token",
        "register_owner",
        "revoke_session",
    },
    "business.py": {
        "create_business",
        "update_business",
        "create_staff",
        "remove_staff",
        "create_service",
        "update_service",
    },
    "queue.py": {"open_today_queue"},
    "retention.py": {"clean"},  # deleting old rows is not a change any client watches
}
EMITTERS = {"emit_event", "emit_token_event", "emit_queue_status", "apply_event"}


def _calls(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            names.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    return names


def test_no_service_function_commits_without_writing_an_event() -> None:
    offenders = []
    for path in sorted(SERVICES.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = _calls(node)
            if "commit" not in calls or calls & EMITTERS:
                continue
            if node.name in ALLOWED_WITHOUT_EVENT.get(path.name, set()):
                continue
            offenders.append(f"{path.name}:{node.name}")
    assert offenders == []


def _row_versions(rows: list[OutboxEvent]) -> dict[str, list[int]]:
    versions: dict[str, list[int]] = {}
    for row in rows:
        if "token_id" in row.payload:
            versions.setdefault(row.payload["token_id"], []).append(row.payload["version"])
    return versions


async def test_token_versions_rise_with_event_ids(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    _, headers = await customer(api)
    token = (await join(api, headers, queue)).json()
    for value in (1, 2, 0):
        current = (await token_row(api, token["id"])).version
        await priority(api, w.owner, token["id"], value, current)
    assert _row_versions(await events(api)) == {token["id"]: [1, 2, 3, 4]}
