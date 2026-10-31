import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, insert, select, text, update

from app.core.security import create_access_token
from app.models import Appointment, AuditLog, Queue, QueueToken, User
from tests.integration.api import Api, open_api
from tests.integration.test_availability import Headers, hours, utc
from tests.integration.test_booking import World, admin, book_one, customer, world
from tests.integration.test_queues import add_token, queue_id, snapshot

pytestmark = pytest.mark.anyio

TOKEN_KEYS = {
    "id",
    "queue_id",
    "token_no",
    "status",
    "priority",
    "source",
    "queued_at",
    "joined_at",
    "version",
    "ahead_count",
}
SLOT = utc(9)  # Monday 2026-11-02 09:00 in Kolkata, as UTC


async def join(api: Api, headers: Headers, queue: str, **extra: str) -> Any:
    return await api.client.post(f"/v1/queues/{queue}/tokens", json={}, headers=headers | extra)


async def check_in(api: Api, headers: Headers, appointment_id: str) -> Any:
    return await api.client.post(f"/v1/appointments/{appointment_id}/check-in", headers=headers)


async def leave(api: Api, headers: Headers, token_id: str) -> Any:
    return await api.client.delete(f"/v1/tokens/{token_id}", headers=headers)


async def set_status(api: Api, headers: Headers, queue: str, status: str, **extra: str) -> Any:
    return await api.client.patch(
        f"/v1/queues/{queue}", json={"status": status}, headers=headers | extra
    )


async def settings(w: World, **values: int) -> None:
    response = await w.api.client.patch(
        f"/v1/businesses/{w.business_id}", json={"settings": values}, headers=w.owner
    )
    assert response.status_code == 200, response.text


async def last_token_no(api: Api, queue: str) -> int:
    async with api.sessions() as session:
        value = await session.scalar(
            select(Queue.last_token_no).where(Queue.id == uuid.UUID(queue))
        )
        return int(value or 0)


async def token_row(api: Api, token_id: str) -> QueueToken:
    async with api.sessions() as session:
        found = await session.get(QueueToken, uuid.UUID(token_id))
        assert found is not None
        return found


async def appointment_status(api: Api, appointment_id: str) -> str:
    async with api.sessions() as session:
        status = await session.scalar(
            select(Appointment.status).where(Appointment.id == uuid.UUID(appointment_id))
        )
        return str(status)


async def audits(api: Api, entity_id: str, action: str) -> list[AuditLog]:
    async with api.sessions() as session:
        rows = await session.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == uuid.UUID(entity_id), AuditLog.action == action
            )
        )
        return list(rows.all())


def code(response: Any) -> str:
    return str(response.json()["code"])


# --- walk-in join ---


async def test_join_returns_the_customer_token_in_order(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    _, first = await customer(api)
    _, second = await customer(api)
    one = await join(api, first, queue)
    assert one.status_code == 201, one.text
    body = one.json()
    assert set(body) == TOKEN_KEYS
    assert (body["token_no"], body["status"], body["source"], body["priority"]) == (
        1,
        "waiting",
        "walk_in",
        0,
    )
    assert body["ahead_count"] == 0 and body["version"] == 1
    assert body["queued_at"] == body["joined_at"] and body["joined_at"].endswith("+05:30")
    api.clock.now += 60  # a later arrival; equal times would fall back to the id tiebreak
    two = (await join(api, second, queue)).json()
    assert (two["token_no"], two["ahead_count"]) == (2, 1)
    assert await last_token_no(api, queue) == 2


async def test_join_roles_and_errors(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    for headers in (w.owner, await w.staff(), await admin(api)):
        assert (await join(api, headers, queue)).status_code == 403
    assert (await api.client.post(f"/v1/queues/{queue}/tokens", json={})).status_code == 401
    _, headers = await customer(api)
    missing = await join(api, headers, str(uuid.uuid4()))
    assert missing.status_code == 404 and code(missing) == "NOT_FOUND"
    extra = await api.client.post(
        f"/v1/queues/{queue}/tokens", json={"priority": 2}, headers=headers
    )
    assert extra.status_code == 422
    assert await last_token_no(api, queue) == 0


async def test_a_failed_join_after_the_increment_never_consumes_a_number(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    _, first = await customer(api)
    _, second = await customer(api)
    assert (await join(api, first, queue)).json()["token_no"] == 1
    # The duplicate fails on one_active_token AFTER last_token_no was incremented in the same
    # transaction; the rollback must give number 2 back.
    again = await join(api, first, queue)
    assert again.status_code == 409 and code(again) == "ALREADY_IN_QUEUE"
    assert await last_token_no(api, queue) == 1
    assert (await join(api, second, queue)).json()["token_no"] == 2


@pytest.mark.parametrize("status", ["paused", "closed"])
async def test_join_refused_when_not_open(api: Api, status: str) -> None:
    w = await world(api)
    queue = await queue_id(w)
    assert (await set_status(api, w.owner, queue, status)).status_code == 200
    _, headers = await customer(api)
    refused = await join(api, headers, queue)
    assert refused.status_code == 409 and code(refused) == "QUEUE_CLOSED"
    if status == "paused":
        await set_status(api, w.owner, queue, "open")
        assert (await join(api, headers, queue)).status_code == 201


@pytest.mark.parametrize(
    ("zone", "last_minute"),
    [
        ("Asia/Kolkata", datetime(2026, 11, 1, 18, 29, tzinfo=UTC)),  # 23:59 on 1 Nov local
        ("America/New_York", datetime(2026, 11, 2, 4, 59, tzinfo=UTC)),  # 23:59 on 1 Nov local
    ],
)
async def test_join_only_into_todays_queue(api: Api, zone: str, last_minute: datetime) -> None:
    w = await world(api, zone=zone)
    api.clock.now = last_minute.timestamp()  # 23:59 local
    queue = await queue_id(w)
    _, early = await customer(api)
    _, late = await customer(api)
    assert (await join(api, early, queue)).status_code == 201
    api.clock.now = (last_minute + timedelta(minutes=1)).timestamp()  # 00:00, a new local day
    refused = await join(api, late, queue)
    assert refused.status_code == 409 and code(refused) == "QUEUE_CLOSED"


async def test_no_shows_do_not_block_walk_ins(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    user, headers = await customer(api)
    async with api.sessions() as session:
        await session.execute(update(User).where(User.id == user.id).values(no_show_count=5))
        await session.commit()
    assert (await join(api, headers, queue)).status_code == 201


async def test_max_walk_ins_counts_walk_ins_that_are_not_cancelled(api: Api) -> None:
    w = await world(api)
    await settings(w, max_walk_ins=2)
    queue = await queue_id(w)
    heads = [(await customer(api))[1] for _ in range(4)]
    first = (await join(api, heads[0], queue)).json()
    assert (await join(api, heads[1], queue)).status_code == 201
    full = await join(api, heads[2], queue)
    assert full.status_code == 409 and code(full) == "WALK_INS_FULL"
    assert (await leave(api, heads[0], first["id"])).status_code == 204  # frees a place
    assert (await join(api, heads[3], queue)).status_code == 201
    assert await last_token_no(api, queue) == 3


async def test_join_idempotent_replay(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    _, headers = await customer(api)
    first = await join(api, headers, queue, **{"Idempotency-Key": "join-1"})
    again = await join(api, headers, queue, **{"Idempotency-Key": "join-1"})
    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == first.json() and again.headers["Idempotent-Replayed"] == "true"
    assert await last_token_no(api, queue) == 1


async def test_join_and_check_in_share_one_rate_limit_bucket(
    migrated_url: str, redis_url: str
) -> None:
    async with open_api(migrated_url, redis_url, {"join_rate_limit_per_hour": 2}) as api:
        w = await world(api)
        _, headers = await customer(api)
        appointment_id = await book_one(w, headers)
        queue = await queue_id(w)
        assert (await join(api, headers, queue)).status_code == 201  # hit 1
        assert (await join(api, headers, queue)).status_code == 409  # hit 2, failures count
        blocked = await check_in(api, headers, appointment_id)  # same bucket
        assert blocked.status_code == 429 and "Retry-After" in blocked.headers


# --- check-in ---


async def booked(w: World, hour: int = 9, minute: int = 0) -> tuple[User, Headers, str]:
    user, headers = await customer(w.api)
    return user, headers, await book_one(w, headers, hour, minute)


@pytest.mark.parametrize(
    ("offset_min", "allowed", "on_time"),
    [
        (-30, True, True),  # the window opens
        (-31, False, None),
        (5, True, True),  # the end of the grace period
        (6, True, False),  # late from here
        (29, True, False),  # one minute before the slot ends
        (30, False, None),  # the slot end (09:30) is closed
    ],
)
async def test_check_in_window_grace_and_queued_at(
    api: Api, offset_min: int, allowed: bool, on_time: bool | None
) -> None:
    w = await world(api)
    _, headers, appointment_id = await booked(w)
    now = SLOT + timedelta(minutes=offset_min)
    api.clock.now = now.timestamp()
    response = await check_in(api, headers, appointment_id)
    if not allowed:
        assert response.status_code == 409 and code(response) == "INVALID_TRANSITION"
        detail = response.json()["detail"]
        assert ("opens" in detail) if offset_min < 0 else ("ended" in detail)
        assert await appointment_status(api, appointment_id) == "booked"
        return
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source"] == "appointment" and body["status"] == "waiting"
    token = await token_row(api, body["id"])
    assert token.joined_at == now
    assert token.queued_at == (SLOT if on_time else now)
    assert token.priority == 0
    assert await appointment_status(api, appointment_id) == "checked_in"


async def test_repeated_check_in_returns_the_same_token_with_200(api: Api) -> None:
    w = await world(api)
    _, headers, appointment_id = await booked(w)
    api.clock.now = SLOT.timestamp()
    first = await check_in(api, headers, appointment_id)
    again = await check_in(api, headers, appointment_id)
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()


async def test_check_in_into_a_paused_queue_is_refused(api: Api) -> None:
    w = await world(api)
    _, headers, appointment_id = await booked(w)
    async with api.sessions() as session:
        await session.execute(
            insert(Queue).values(
                business_id=w.business_id,
                resource_id=w.resource,
                queue_date=date(2026, 11, 2),
                status="paused",
            )
        )
        await session.commit()
    api.clock.now = SLOT.timestamp()
    refused = await check_in(api, headers, appointment_id)
    assert refused.status_code == 409 and code(refused) == "QUEUE_CLOSED"
    assert await appointment_status(api, appointment_id) == "booked"


async def test_near_midnight_check_in_uses_the_slots_day(api: Api) -> None:
    w = await world(api, open_hours=hours(("00:00", "23:59")))
    _, headers, appointment_id = await booked(w, 0, 15)  # 00:15 local on Monday 2 Nov
    api.clock.now = utc(23, 50, day=1).timestamp()  # 23:50 local on Sunday 1 Nov
    response = await check_in(api, headers, appointment_id)
    assert response.status_code == 201, response.text
    async with api.sessions() as session:
        day = await session.scalar(
            select(Queue.queue_date).where(Queue.id == uuid.UUID(response.json()["queue_id"]))
        )
    assert day == date(2026, 11, 2)
    _, walker = await customer(api)  # tomorrow's queue is not open to walk-ins yet
    refused = await join(api, walker, response.json()["queue_id"])
    assert refused.status_code == 409 and code(refused) == "QUEUE_CLOSED"


async def test_staff_check_in_is_audited_and_customer_check_in_is_not(api: Api) -> None:
    w = await world(api)
    staff = await w.staff()
    _, mine, by_me = await booked(w, 9)
    _, _, by_staff = await booked(w, 10)
    api.clock.now = SLOT.timestamp()
    assert (await check_in(api, mine, by_me)).status_code == 201
    api.clock.now = (SLOT + timedelta(hours=1)).timestamp()
    assert (await check_in(api, staff, by_staff)).status_code == 201
    assert await audits(api, by_me, "appointment.check_in") == []
    (row,) = await audits(api, by_staff, "appointment.check_in")
    assert row.business_id == w.business_id


async def test_check_in_roles_and_states(api: Api) -> None:
    w = await world(api)
    other = await world(api)
    _, headers, appointment_id = await booked(w)
    api.clock.now = SLOT.timestamp()
    _, stranger = await customer(api)
    missing = await check_in(api, stranger, str(uuid.uuid4()))
    assert missing.status_code == 404
    for outsider in (stranger, other.owner, await other.staff()):
        response = await check_in(api, outsider, appointment_id)
        assert response.status_code == 404 and response.json() == missing.json()
    assert (await check_in(api, await admin(api), appointment_id)).status_code == 403
    assert (await api.client.post(f"/v1/appointments/{appointment_id}/check-in")).status_code == 401
    assert (await check_in(api, w.owner, appointment_id)).status_code == 201
    _, other_headers, cancelled = await booked(w, 11)
    await api.client.delete(f"/v1/appointments/{cancelled}", headers=other_headers)
    refused = await check_in(api, other_headers, cancelled)
    assert refused.status_code == 409 and code(refused) == "INVALID_TRANSITION"


async def test_check_in_with_a_walk_in_token_already_held_is_refused(api: Api) -> None:
    w = await world(api)
    _, headers, appointment_id = await booked(w)
    api.clock.now = SLOT.timestamp()  # Monday, so Monday's queue is today's
    queue = await queue_id(w)
    assert (await join(api, headers, queue)).status_code == 201
    refused = await check_in(api, headers, appointment_id)
    assert refused.status_code == 409 and code(refused) == "ALREADY_IN_QUEUE"
    assert await appointment_status(api, appointment_id) == "booked"


async def test_appointment_tokens_do_not_count_towards_max_walk_ins(api: Api) -> None:
    w = await world(api)
    await settings(w, max_walk_ins=0)
    _, headers, appointment_id = await booked(w)
    api.clock.now = SLOT.timestamp()
    assert (await check_in(api, headers, appointment_id)).status_code == 201
    _, walker = await customer(api)
    full = await join(api, walker, await queue_id(w))
    assert code(full) == "WALK_INS_FULL"


# --- leave ---


async def test_leave_frees_the_place_and_the_customer_can_join_again(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    _, headers = await customer(api)
    first = (await join(api, headers, queue)).json()
    left = await leave(api, headers, first["id"])
    assert left.status_code == 204 and left.content == b""
    token = await token_row(api, first["id"])
    assert (token.status, token.version) == ("cancelled", 2)
    assert (await leave(api, headers, first["id"])).status_code == 204  # leaving twice
    again = await join(api, headers, queue)
    assert again.status_code == 201 and again.json()["token_no"] == 2


@pytest.mark.parametrize(
    ("status", "expected"),
    [("called", 204), ("in_service", 409), ("completed", 409), ("no_show", 409)],
)
async def test_leave_by_status(api: Api, status: str, expected: int) -> None:
    w = await world(api)
    queue = await queue_id(w)
    token_id = await add_token(api, w, queue, 1, status)
    owner_id = (await token_row(api, token_id)).customer_id
    headers = {"Authorization": f"Bearer {_customer_token(api, owner_id)}"}
    response = await leave(api, headers, token_id)
    assert response.status_code == expected
    if expected == 409:
        assert code(response) == "INVALID_TRANSITION"
        assert (await token_row(api, token_id)).status == status


def _customer_token(api: Api, user_id: uuid.UUID) -> str:
    return create_access_token(api.settings, user_id, "customer")


async def test_leave_roles_and_strangers(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    _, headers = await customer(api)
    token = (await join(api, headers, queue)).json()["id"]
    _, stranger = await customer(api)
    missing = await leave(api, stranger, str(uuid.uuid4()))
    theirs = await leave(api, stranger, token)
    assert missing.status_code == theirs.status_code == 404
    assert missing.json() == theirs.json()
    for role in (w.owner, await w.staff(), await admin(api)):
        assert (await leave(api, role, token)).status_code == 403
    assert (await api.client.delete(f"/v1/tokens/{token}")).status_code == 401
    assert (await token_row(api, token)).status == "waiting"


async def test_leaving_an_appointment_token_cancels_the_appointment(api: Api) -> None:
    w = await world(api)
    _, headers, appointment_id = await booked(w)
    api.clock.now = SLOT.timestamp()
    token = (await check_in(api, headers, appointment_id)).json()["id"]
    assert (await leave(api, headers, token)).status_code == 204
    assert await appointment_status(api, appointment_id) == "cancelled"


async def test_leave_loses_to_a_concurrent_change_with_version_conflict(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    _, headers = await customer(api)
    token = (await join(api, headers, queue)).json()["id"]
    async with api.sessions() as holder:  # a staff-like change, uncommitted
        await holder.execute(
            update(QueueToken)
            .where(QueueToken.id == uuid.UUID(token))
            .values(status="called", version=QueueToken.version + 1)
        )
        task = asyncio.create_task(leave(api, headers, token))
        async with api.sessions() as probe:
            for _ in range(20000):
                waiting = await probe.scalar(
                    text("SELECT count(*) FROM pg_locks WHERE NOT granted")
                )
                await probe.rollback()
                if waiting:
                    break
                await asyncio.sleep(0)
            else:
                pytest.fail("the leave never waited on the row")
        await holder.commit()
    response = await task
    assert response.status_code == 409 and code(response) == "VERSION_CONFLICT"
    assert (await token_row(api, token)).status == "called"


# --- queue status ---


async def test_queue_status_moves_audits_and_snapshot(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    for status in ("paused", "open", "paused", "closed"):
        response = await set_status(api, w.owner, queue, status)
        assert response.status_code == 200 and response.json()["status"] == status
    assert len(await audits(api, queue, "queue.status")) == 4
    for status in ("open", "paused"):
        refused = await set_status(api, w.owner, queue, status)
        assert refused.status_code == 409 and code(refused) == "INVALID_TRANSITION"
    same = await set_status(api, w.owner, queue, "closed")  # no-op
    assert same.status_code == 200
    assert len(await audits(api, queue, "queue.status")) == 4
    assert (await snapshot(api, w.owner, queue)).json()["status"] == "closed"


async def test_queue_status_roles_and_validation(api: Api) -> None:
    w = await world(api)
    other = await world(api)
    queue = await queue_id(w)
    _, customer_headers = await customer(api)
    for headers in (customer_headers, await admin(api)):
        assert (await set_status(api, headers, queue, "paused")).status_code == 403
    assert (await set_status(api, other.owner, queue, "paused")).status_code == 404
    assert (await set_status(api, await w.staff(), queue, "paused")).status_code == 200
    assert (await set_status(api, w.owner, queue, "busy")).status_code == 422
    replay = await set_status(api, w.owner, queue, "open", **{"Idempotency-Key": "q-1"})
    again = await set_status(api, w.owner, queue, "open", **{"Idempotency-Key": "q-1"})
    assert replay.status_code == 200 and again.headers["Idempotent-Replayed"] == "true"


# --- GET /tokens?active=true ---


async def test_active_tokens_are_own_only_with_ahead_count(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    mine = await add_token(api, w, queue, 1, "waiting", 0, utc(3, 30))
    await add_token(api, w, queue, 2, "waiting", 0, utc(2, 30))  # ahead: same tier, earlier
    await add_token(api, w, queue, 3, "waiting", 1, utc(4, 0))  # ahead: higher priority
    await add_token(api, w, queue, 4, "waiting", 0, utc(4, 30))  # behind
    await add_token(api, w, queue, 5, "called", 2, utc(1, 0))  # not waiting: not counted
    owner_id = (await token_row(api, mine)).customer_id
    headers = {"Authorization": f"Bearer {_customer_token(api, owner_id)}"}
    other = await world(api)
    called = await add_token(
        api, other, await queue_id(other), 1, "called", 0, utc(3), customer_id=owner_id
    )
    response = await api.client.get("/v1/tokens", params={"active": "true"}, headers=headers)
    assert response.status_code == 200, response.text
    items = response.json()
    assert [item["id"] for item in items] == [mine, called] or [item["id"] for item in items] == [
        called,
        mine,
    ]
    by_id = {item["id"]: item for item in items}
    assert by_id[mine]["ahead_count"] == 2 and by_id[called]["ahead_count"] is None
    assert set(by_id[mine]) == TOKEN_KEYS | {"business_id", "queue_date", "queue_status"}
    assert by_id[mine]["queue_date"] == "2026-11-01" and by_id[mine]["queue_status"] == "open"


async def test_active_tokens_validation_roles_and_statement_count(api: Api) -> None:
    w = await world(api)
    _, headers = await customer(api)
    for params in ({}, {"active": "false"}, {"active": "1"}):
        bad = await api.client.get("/v1/tokens", params=params, headers=headers)
        assert bad.status_code == 422 and bad.json()["errors"][0]["loc"] == ["query", "active"]
    assert (
        await api.client.get("/v1/tokens", params={"active": "true"}, headers=w.owner)
    ).status_code == 403
    queue = await queue_id(w)
    await join(api, headers, queue)
    statements: list[str] = []
    engine = api.app.state.engine.sync_engine

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = await api.client.get("/v1/tokens", params={"active": "true"}, headers=headers)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(response.json()) == 1
    assert len(statements) == 2  # the role check and one token query


# --- timezone guard ---


async def test_timezone_change_is_refused_while_tokens_are_active(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    token = await add_token(api, w, queue, 1, "waiting")
    url = f"/v1/businesses/{w.business_id}"
    refused = await api.client.patch(url, json={"timezone": "Asia/Dubai"}, headers=w.owner)
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["loc"] == ["body", "timezone"]
    async with api.sessions() as session:
        await session.execute(
            update(QueueToken)
            .where(QueueToken.id == uuid.UUID(token))
            .values(status="completed", completed_at=datetime.now(UTC))
        )
        await session.commit()
    allowed = await api.client.patch(url, json={"timezone": "Asia/Dubai"}, headers=w.owner)
    assert allowed.status_code == 200, allowed.text
