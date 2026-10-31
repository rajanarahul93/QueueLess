import asyncio
import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects import postgresql

from app.core.errors import Unauthenticated
from app.core.security import create_stream_token, verify_stream_token
from app.models import Appointment, AuditLog, Queue, QueueToken, ServiceSession, User
from app.services.queue import active_tokens_query, next_waiting_query
from tests.integration.api import Api
from tests.integration.test_availability import Headers, utc
from tests.integration.test_booking import World, admin, book_one, customer, world
from tests.integration.test_queues import add_token, queue_id, snapshot
from tests.integration.test_tokens import SLOT, check_in, join, set_status, token_row

pytestmark = pytest.mark.anyio

STAFF_KEYS = {
    "id",
    "token_no",
    "status",
    "priority",
    "source",
    "skip_count",
    "version",
    "joined_at",
    "queued_at",
    "called_at",
    "started_at",
    "order_position",
    "appointment_id",
    "customer",
}


def code(response: Any) -> str:
    return str(response.json()["code"])


async def call_next(api: Api, headers: Headers, queue: str, **extra: str) -> Any:
    return await api.client.post(f"/v1/queues/{queue}/call-next", headers=headers | extra)


async def act(
    api: Api, headers: Headers, token: str, action: str, version: int, **body: object
) -> Any:
    return await api.client.post(
        f"/v1/tokens/{token}/{action}", json={"version": version, **body}, headers=headers
    )


async def priority(api: Api, headers: Headers, token: str, value: int, version: int) -> Any:
    return await api.client.patch(
        f"/v1/tokens/{token}/priority",
        json={"priority": value, "version": version},
        headers=headers,
    )


async def audit_count(api: Api, token: str, action: str | None = None) -> int:
    async with api.sessions() as session:
        query = (
            select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == uuid.UUID(token))
        )
        if action:
            query = query.where(AuditLog.action == action)
        return int(await session.scalar(query) or 0)


async def sessions_of(api: Api, token: str) -> list[ServiceSession]:
    async with api.sessions() as session:
        rows = await session.scalars(
            select(ServiceSession).where(ServiceSession.token_id == uuid.UUID(token))
        )
        return list(rows.all())


async def no_shows(api: Api, user_id: uuid.UUID) -> int:
    async with api.sessions() as session:
        return int(await session.scalar(select(User.no_show_count).where(User.id == user_id)) or 0)


async def appointment_status(api: Api, appointment_id: str) -> str:
    async with api.sessions() as session:
        return str(
            await session.scalar(
                select(Appointment.status).where(Appointment.id == uuid.UUID(appointment_id))
            )
        )


async def appointment_token(w: World) -> tuple[User, str, str]:
    """A checked-in appointment token on Monday's queue; the clock is at the slot start."""
    user, headers = await customer(w.api)
    appointment_id = await book_one(w, headers)
    w.api.clock.now = SLOT.timestamp()
    response = await check_in(w.api, headers, appointment_id)
    assert response.status_code == 201, response.text
    return user, appointment_id, str(response.json()["id"])


# --- call-next ---


async def test_call_next_takes_the_base_order_and_returns_the_staff_shape(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    low_early = await add_token(api, w, queue, 1, "waiting", 0, utc(3))
    high_late = await add_token(api, w, queue, 2, "waiting", 2, utc(5))
    low_late = await add_token(api, w, queue, 3, "waiting", 0, utc(4))
    called = []
    for _ in range(3):
        response = await call_next(api, w.owner, queue)
        assert response.status_code == 200, response.text
        called.append(response.json())
    assert [c["id"] for c in called] == [high_late, low_early, low_late]
    first = called[0]
    assert set(first) == STAFF_KEYS and set(first["customer"]) == {"id", "name"}
    assert (first["status"], first["version"], first["order_position"]) == ("called", 2, None)
    assert first["called_at"].endswith("+05:30")
    assert await audit_count(api, high_late, "token.call") == 1
    empty = await call_next(api, w.owner, queue)
    assert empty.status_code == 204 and empty.content == b""


async def test_equal_queued_at_is_called_in_token_number_order(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    same = utc(3)
    numbers = (4, 1, 6, 3, 5, 2)  # inserted out of order; a random tiebreak passes 1 in 720
    ids = {no: await add_token(api, w, queue, no, "waiting", 0, same) for no in numbers}
    order = [(await call_next(api, w.owner, queue)).json()["id"] for _ in numbers]
    assert order == [ids[no] for no in sorted(numbers)]


async def test_call_next_roles_status_and_today(api: Api) -> None:
    w = await world(api)
    other = await world(api)
    queue = await queue_id(w)
    for no in range(1, 4):
        await add_token(api, w, queue, no, "waiting")
    _, customer_headers = await customer(api)
    for headers in (customer_headers, await admin(api)):
        assert (await call_next(api, headers, queue)).status_code == 403
    assert (await call_next(api, other.owner, queue)).status_code == 404
    assert (await api.client.post(f"/v1/queues/{queue}/call-next")).status_code == 401
    for status in ("paused", "closed"):  # staff keep serving a paused or closed queue
        await set_status(api, w.owner, queue, status)
        assert (await call_next(api, await w.staff(), queue)).status_code == 200
    api.clock.now += 86_400  # tomorrow: this is no longer today's queue
    refused = await call_next(api, w.owner, queue)
    assert refused.status_code == 409 and code(refused) == "QUEUE_CLOSED"
    assert refused.json()["detail"] == "This is not today's queue"


async def test_a_replayed_call_next_returns_the_same_token(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    for no in (1, 2):
        await add_token(api, w, queue, no, "waiting", 0, utc(3, no))
    first = await call_next(api, w.owner, queue, **{"Idempotency-Key": "press-1"})
    again = await call_next(api, w.owner, queue, **{"Idempotency-Key": "press-1"})
    assert again.json() == first.json() and again.headers["Idempotent-Replayed"] == "true"
    next_one = await call_next(api, w.owner, queue, **{"Idempotency-Key": "press-2"})
    assert next_one.json()["id"] != first.json()["id"]


# --- start, complete, skip, no-show ---


async def test_walk_in_start_needs_an_active_service_of_the_business(api: Api) -> None:
    w = await world(api)
    other = await world(api)
    queue = await queue_id(w)
    token = await add_token(api, w, queue, 1, "called")
    missing = await act(api, w.owner, token, "start", 1)
    assert missing.status_code == 422
    assert missing.json()["errors"][0]["loc"] == ["body", "service_id"]
    foreign = await act(api, w.owner, token, "start", 1, service_id=other.service_id)
    assert foreign.status_code == 404
    await api.client.patch(
        f"/v1/businesses/{w.business_id}/services/{w.service_id}",
        json={"is_active": False},
        headers=w.owner,
    )
    inactive = await act(api, w.owner, token, "start", 1, service_id=w.service_id)
    assert inactive.status_code == 404
    await api.client.patch(
        f"/v1/businesses/{w.business_id}/services/{w.service_id}",
        json={"is_active": True},
        headers=w.owner,
    )
    assert await sessions_of(api, token) == [] and await audit_count(api, token) == 0
    started = await act(api, w.owner, token, "start", 1, service_id=w.service_id)
    assert started.status_code == 200, started.text
    assert (started.json()["status"], started.json()["version"]) == ("in_service", 2)
    (row,) = await sessions_of(api, token)
    assert str(row.service_id) == w.service_id and row.resource_id == w.resource
    assert row.started_at == datetime.fromtimestamp(api.clock.now, UTC) and row.ended_at is None


async def test_appointment_token_full_service_cycle(api: Api) -> None:
    w = await world(api)
    _, appointment_id, token = await appointment_token(w)
    queue = (await token_row(api, token)).queue_id
    called = await call_next(api, w.owner, str(queue))
    assert called.json()["id"] == token
    refused = await act(api, w.owner, token, "start", 2, service_id=w.service_id)
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["loc"] == ["body", "service_id"]
    assert (await act(api, w.owner, token, "start", 2)).status_code == 200
    assert await appointment_status(api, appointment_id) == "in_service"
    (row,) = await sessions_of(api, token)
    assert str(row.service_id) == w.service_id
    api.clock.now += 754  # 12 min 34 s of service
    done = await act(api, w.owner, token, "complete", 3)
    assert done.status_code == 200 and done.json()["status"] == "completed"
    (row,) = await sessions_of(api, token)
    assert row.duration_sec == 754 and row.ended_at is not None
    assert await appointment_status(api, appointment_id) == "completed"
    assert [await audit_count(api, token, f"token.{a}") for a in ("call", "start", "complete")] == [
        1,
        1,
        1,
    ]


async def test_skips_then_the_third_skip_is_a_no_show(api: Api) -> None:
    w = await world(api)
    user, appointment_id, token = await appointment_token(w)
    queue = str((await token_row(api, token)).queue_id)
    version = 1
    for skips in (1, 2):
        assert (await call_next(api, w.owner, queue)).json()["id"] == token
        version += 1
        skipped = await act(api, w.owner, token, "skip", version)
        assert skipped.status_code == 200, skipped.text
        version += 1
        body = skipped.json()
        assert (body["status"], body["skip_count"], body["called_at"]) == ("waiting", skips, None)
        assert (
            body["queued_at"]
            == datetime.fromtimestamp(api.clock.now, UTC).astimezone(body_zone(body)).isoformat()
        )
        assert body["order_position"] == 1
        api.clock.now += 60
    assert (await call_next(api, w.owner, queue)).json()["id"] == token
    version += 1
    third = await act(api, w.owner, token, "skip", version)
    assert third.json()["status"] == "no_show" and third.json()["skip_count"] == 3
    assert await no_shows(api, user.id) == 1
    assert await appointment_status(api, appointment_id) == "no_show"
    assert await sessions_of(api, token) == []


def body_zone(body: dict[str, Any]) -> Any:
    return datetime.fromisoformat(body["joined_at"]).tzinfo


async def test_manual_no_show(api: Api) -> None:
    w = await world(api)
    user, appointment_id, token = await appointment_token(w)
    queue = str((await token_row(api, token)).queue_id)
    await call_next(api, w.owner, queue)
    marked = await act(api, await w.staff(), token, "no-show", 2)
    assert marked.status_code == 200 and marked.json()["status"] == "no_show"
    assert await no_shows(api, user.id) == 1
    assert await appointment_status(api, appointment_id) == "no_show"
    assert await audit_count(api, token, "token.mark_no_show") == 1


async def test_version_then_state_errors(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    token = await add_token(api, w, queue, 1, "waiting")
    stale = await act(api, w.owner, token, "complete", 7)  # stale AND illegal: version first
    assert stale.status_code == 409 and code(stale) == "VERSION_CONFLICT"
    illegal = await act(api, w.owner, token, "complete", 1)
    assert illegal.status_code == 409 and code(illegal) == "INVALID_TRANSITION"
    for action in ("start", "skip", "no-show"):  # none is legal on a waiting token
        response = await act(
            api,
            w.owner,
            token,
            action,
            1,
            **({"service_id": w.service_id} if action == "start" else {}),
        )
        assert code(response) == "INVALID_TRANSITION"
    assert (await token_row(api, token)).version == 1
    assert await audit_count(api, token) == 0
    extra = await api.client.post(
        f"/v1/tokens/{token}/skip", json={"version": 1, "priority": 2}, headers=w.owner
    )
    assert extra.status_code == 422


@pytest.mark.parametrize(
    ("status", "legal"),
    [
        ("waiting", set()),
        ("called", {"start", "skip", "no-show"}),
        ("in_service", {"complete"}),
        ("completed", set()),
        ("no_show", set()),
        ("cancelled", set()),
    ],
)
async def test_every_action_in_every_status(api: Api, status: str, legal: set[str]) -> None:
    w = await world(api)
    queue = await queue_id(w)
    for action in ("start", "complete", "skip", "no-show"):
        token = await add_token(api, w, queue, 100 + len(action), status)
        extra = {"service_id": w.service_id} if action == "start" else {}
        response = await act(api, w.owner, token, action, 1, **extra)
        if action in legal:
            assert response.status_code == 200, (action, response.text)
            assert await audit_count(api, token) == 1
        else:
            assert response.status_code == 409 and code(response) == "INVALID_TRANSITION"
            assert await audit_count(api, token) == 0
        async with api.sessions() as session:  # free the token number for the next action
            await session.execute(
                update(QueueToken)
                .where(QueueToken.id == uuid.UUID(token))
                .values(token_no=QueueToken.token_no + 1000, status="cancelled")
            )
            await session.commit()


async def test_token_action_roles(api: Api) -> None:
    w = await world(api)
    other = await world(api)
    queue = await queue_id(w)
    token = await add_token(api, w, queue, 1, "called")
    _, customer_headers = await customer(api)
    for headers in (customer_headers, await admin(api)):
        assert (await act(api, headers, token, "skip", 1)).status_code == 403
    missing = await act(api, other.owner, str(uuid.uuid4()), "skip", 1)
    for stranger in (other.owner, await other.staff()):
        response = await act(api, stranger, token, "skip", 1)
        assert response.status_code == 404 and response.json() == missing.json()
    assert (
        await api.client.post(f"/v1/tokens/{token}/skip", json={"version": 1})
    ).status_code == 401
    assert (await act(api, await w.staff(), token, "skip", 1)).status_code == 200


async def test_a_token_in_yesterdays_queue_can_still_be_finished(api: Api) -> None:
    w = await world(api)
    async with api.sessions() as session:
        yesterday = await session.scalar(
            insert(Queue)
            .values(
                business_id=w.business_id,
                resource_id=w.resource,
                queue_date=date(2026, 10, 31),
                status="open",
            )
            .returning(Queue.id)
        )
        await session.commit()
    token = await add_token(api, w, str(yesterday), 1, "called")
    assert (await act(api, w.owner, token, "start", 1, service_id=w.service_id)).status_code == 200
    assert (await act(api, w.owner, token, "complete", 2)).status_code == 200


# --- the read-then-update window ---


async def change_underneath(api: Api, token: str, request: Any) -> Any:
    """Bump the token's version in another uncommitted transaction, let the request read the
    old version and block on the row, then commit: the request must lose cleanly."""
    async with api.sessions() as holder:
        await holder.execute(
            update(QueueToken)
            .where(QueueToken.id == uuid.UUID(token))
            .values(version=QueueToken.version + 1)
        )
        task = asyncio.create_task(request)
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
                pytest.fail("the request never waited on the token row")
        await holder.commit()
    return await task


async def test_a_change_between_read_and_update_leaves_no_partial_start(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    token = await add_token(api, w, queue, 1, "called")
    response = await change_underneath(
        api, token, act(api, w.owner, token, "start", 1, service_id=w.service_id)
    )
    assert response.status_code == 409 and code(response) == "VERSION_CONFLICT"
    assert (await token_row(api, token)).status == "called"
    assert await sessions_of(api, token) == [] and await audit_count(api, token) == 0


async def test_a_change_between_read_and_update_leaves_no_partial_no_show(api: Api) -> None:
    w = await world(api)
    user, appointment_id, token = await appointment_token(w)
    async with api.sessions() as session:
        await session.execute(
            update(QueueToken)
            .where(QueueToken.id == uuid.UUID(token))
            .values(status="called", skip_count=2)
        )
        await session.commit()
    response = await change_underneath(api, token, act(api, w.owner, token, "skip", 1))
    assert response.status_code == 409 and code(response) == "VERSION_CONFLICT"
    assert await no_shows(api, user.id) == 0
    assert await appointment_status(api, appointment_id) == "checked_in"
    assert (await token_row(api, token)).status == "called"
    assert await audit_count(api, token, "token.skip") == 0


# --- priority ---


async def test_priority_rules(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    token = await add_token(api, w, queue, 1, "waiting")
    raised = await priority(api, w.owner, token, 2, 1)
    assert raised.status_code == 200 and (raised.json()["priority"], raised.json()["version"]) == (
        2,
        2,
    )
    same = await priority(api, w.owner, token, 2, 2)  # no-op
    assert same.status_code == 200 and same.json()["version"] == 2
    assert await audit_count(api, token, "token.priority") == 1
    assert (await priority(api, w.owner, token, 3, 2)).status_code == 422
    assert code(await priority(api, w.owner, token, 1, 1)) == "VERSION_CONFLICT"
    called = await add_token(api, w, queue, 2, "called")
    assert code(await priority(api, w.owner, called, 1, 1)) == "INVALID_TRANSITION"


# --- stream token ---


async def test_stream_token_claims_and_access(api: Api) -> None:
    w = await world(api)
    other = await world(api)
    queue = await queue_id(w)
    staff = await w.staff()
    response = await api.client.post(f"/v1/queues/{queue}/stream-token", headers=staff)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expires_in"] == api.settings.stream_token_ttl_sec
    claims = verify_stream_token(api.settings, body["stream_token"], "queue", uuid.UUID(queue))
    assert claims.resource_id == uuid.UUID(queue) and claims.scope == "queue"
    with pytest.raises(Unauthenticated):
        verify_stream_token(api.settings, body["stream_token"], "queue", uuid.uuid4())
    with pytest.raises(Unauthenticated):
        verify_stream_token(api.settings, body["stream_token"], "token", uuid.UUID(queue))
    _, customer_headers = await customer(api)
    for headers in (customer_headers, await admin(api)):
        assert (
            await api.client.post(f"/v1/queues/{queue}/stream-token", headers=headers)
        ).status_code == 403
    assert (
        await api.client.post(f"/v1/queues/{queue}/stream-token", headers=other.owner)
    ).status_code == 404


async def test_an_expired_stream_token_is_rejected(api: Api) -> None:
    w = await world(api)
    queue = uuid.UUID(await queue_id(w))
    old = datetime.now(UTC) - timedelta(seconds=api.settings.stream_token_ttl_sec + 120)
    expired = create_stream_token(api.settings, "queue", queue, uuid.uuid4(), now=old)
    with pytest.raises(Unauthenticated):
        verify_stream_token(api.settings, expired, "queue", queue)


# --- privacy and the snapshot ---


async def test_snapshot_follows_every_change_and_keeps_privacy(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    first = await add_token(api, w, queue, 1, "waiting", 0, utc(3))
    second = await add_token(api, w, queue, 2, "waiting", 0, utc(4))
    await call_next(api, w.owner, queue)
    await act(api, w.owner, first, "start", 2, service_id=w.service_id)
    await priority(api, w.owner, second, 2, 1)
    body = (await snapshot(api, w.owner, queue)).json()
    assert [(t["id"], t["status"], t["priority"]) for t in body["tokens"]] == [
        (second, "waiting", 2),
        (first, "in_service", 0),
    ]
    raw = json.dumps(body)
    for forbidden in ("+91", "@example", "argon2", "phone", "email", "password"):
        assert forbidden not in raw


async def test_customer_responses_never_carry_other_customers_data(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    other = await add_token(api, w, queue, 900, "waiting", 0, utc(3))  # clear of joins
    await priority(api, w.owner, other, 2, 1)  # another customer's staff-set priority
    other_row = await token_row(api, other)
    _, headers = await customer(api)
    joined = (await join(api, headers, queue)).json()
    listed = (await api.client.get("/v1/tokens", params={"active": "true"}, headers=headers)).json()
    for body in (joined, *listed):
        raw = json.dumps(body)
        assert other not in raw and str(other_row.customer_id) not in raw
        assert body["ahead_count"] == 1  # a count, never who is ahead or why
        assert "customer" not in body


# --- the index ---


async def test_snapshot_and_call_next_use_the_active_tokens_index(api: Api) -> None:
    """Realistic shape: 40 past daily queues of 300 tokens each, plus today's queue with 300
    (a third of them active). On a table this size a queue-scoped read must use the index."""
    w = await world(api)
    queue = uuid.UUID(await queue_id(w))
    ids = {"b": w.business_id, "r": w.resource, "q": queue}
    async with api.sessions() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, name, role, phone) SELECT gen_random_uuid(), 'u', "
                "'customer', '+7' || lpad(g::text, 11, '0') FROM generate_series(1, 12300) g"
            )
        )
        await session.execute(
            text(
                "INSERT INTO queues (id, business_id, resource_id, queue_date, status) "
                "SELECT gen_random_uuid(), :b, :r, date '2026-01-01' + g, 'closed' "
                "FROM generate_series(1, 40) g"
            ),
            ids,
        )
        await session.execute(
            text(
                "WITH q AS (SELECT id, row_number() OVER (ORDER BY queue_date) AS n FROM queues "
                "WHERE business_id = :b AND id <> :q), "
                "u AS (SELECT id, row_number() OVER (ORDER BY phone) AS n FROM users "
                "WHERE phone LIKE '+7%') "
                "INSERT INTO queue_tokens (business_id, queue_id, token_no, source, customer_id, "
                "status, queued_at) "
                "SELECT :b, q.id, ((u.n - 1) % 300) + 1, 'walk_in', u.id, 'completed', now() "
                "FROM u JOIN q ON q.n = ((u.n - 1) / 300) + 1 WHERE u.n <= 12000"
            ),
            ids,
        )
        await session.execute(
            text(
                "WITH u AS (SELECT id, row_number() OVER (ORDER BY phone) AS n FROM users "
                "WHERE phone LIKE '+7%') "
                "INSERT INTO queue_tokens (business_id, queue_id, token_no, source, customer_id, "
                "status, queued_at) "
                "SELECT :b, :q, u.n - 12000, 'walk_in', u.id, "
                "(ARRAY['waiting', 'completed', 'cancelled'])[((u.n - 12000) % 3) + 1], "
                "now() + make_interval(secs => u.n) FROM u WHERE u.n > 12000"
            ),
            ids,
        )
        await session.commit()
        await session.execute(text("ANALYZE queue_tokens"))
        version = await session.scalar(text("SHOW server_version"))
        total, today, active = (
            await session.execute(
                text(
                    "SELECT count(*), count(*) FILTER (WHERE queue_id = :q), "
                    "count(*) FILTER (WHERE queue_id = :q "
                    "AND status IN ('waiting', 'called', 'in_service')) FROM queue_tokens"
                ),
                ids,
            )
        ).one()
        plans: dict[str, str] = {}
        for name, query in (
            ("snapshot", active_tokens_query(queue)),
            ("call_next", next_waiting_query(queue)),
        ):
            compiled = query.compile(
                dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
                compile_kwargs={"literal_binds": True},
            )
            rows = await session.execute(text(f"EXPLAIN {compiled}"))
            plans[name] = "\n".join(row[0] for row in rows)
    print(
        f"\nPostgres {version}; queue_tokens rows={total}, today's queue={today}, active={active}\n"
        + "\n\n".join(f"{name}:\n{plan}" for name, plan in plans.items())
    )
    for plan in plans.values():
        # An Index Scan or Bitmap Index Scan on the index; node shapes vary by version.
        assert re.search(r"Index Scan (using|on) active_tokens", plan), plan
        assert "Seq Scan on queue_tokens" not in plan, plan


async def test_priority_is_only_ever_in_staff_responses(api: Api) -> None:
    w = await world(api)
    _, _, checked_in = await appointment_token(w)
    queue = str((await token_row(api, checked_in)).queue_id)
    _, headers = await customer(api)
    joined = await join(api, headers, queue)
    mine = joined.json()["id"]
    raised = await priority(api, w.owner, mine, 2, 1)  # staff raise it to the top tier
    assert raised.json()["priority"] == 2  # staff action response
    staff_view = (await snapshot(api, w.owner, queue)).json()["tokens"]
    assert all("priority" in token for token in staff_view)
    _, booker = await customer(api)
    second_appointment = await book_one(w, booker, 11)
    api.clock.now = (SLOT + timedelta(hours=2)).timestamp()
    checked = await check_in(api, booker, second_appointment)
    replay = await check_in(api, booker, second_appointment)  # the 200 path
    listed = await api.client.get("/v1/tokens", params={"active": "true"}, headers=headers)
    customer_bodies = [joined.json(), checked.json(), replay.json(), *listed.json()]
    assert len(customer_bodies) == 4 and listed.json()[0]["id"] == mine
    for body in customer_bodies:
        assert "priority" not in body, body
