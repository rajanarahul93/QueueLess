import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event, func, insert, select, update

from app.models import Queue, QueueToken, Resource
from tests.integration.api import Api, new_email
from tests.integration.test_availability import Headers
from tests.integration.test_booking import World, admin, customer, world
from tests.integration.test_business_admin import PASSWORD, login, new_staff

pytestmark = pytest.mark.anyio

QUEUE_KEYS = {"id", "business_id", "resource_id", "queue_date", "status"}


async def lookup(api: Api, headers: Headers, business_id: uuid.UUID) -> Any:
    return await api.client.get(f"/v1/businesses/{business_id}/queue", headers=headers)


async def queue_id(w: World) -> str:
    response = await lookup(w.api, w.owner, w.business_id)
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def snapshot(api: Api, headers: Headers, queue: str) -> Any:
    return await api.client.get(f"/v1/queues/{queue}", headers=headers)


async def queue_rows(api: Api, business_id: uuid.UUID) -> int:
    async with api.sessions() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(Queue).where(Queue.business_id == business_id)
            )
            or 0
        )


def utc(hour: int, minute: int = 0, day: int = 2) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


# --- GET /businesses/{id}/queue ---


async def test_every_authenticated_role_gets_the_same_queue(api: Api) -> None:
    w = await world(api)  # Asia/Kolkata; the clock is 2026-11-01 11:30 local
    _, customer_headers = await customer(api)
    bodies = [
        (await lookup(api, headers, w.business_id)).json()
        for headers in (w.owner, await w.staff(), customer_headers, await admin(api))
    ]
    assert all(body == bodies[0] for body in bodies)
    assert set(bodies[0]) == QUEUE_KEYS  # never tokens or customer data
    assert bodies[0]["queue_date"] == "2026-11-01" and bodies[0]["status"] == "open"
    assert bodies[0]["business_id"] == str(w.business_id)
    assert bodies[0]["resource_id"] == str(w.resource)
    assert await queue_rows(api, w.business_id) == 1


async def test_lookup_errors(api: Api) -> None:
    w = await world(api)
    assert (await api.client.get(f"/v1/businesses/{w.business_id}/queue")).status_code == 401
    unknown = await lookup(api, w.owner, uuid.uuid4())
    assert unknown.status_code == 404 and unknown.json()["code"] == "NOT_FOUND"
    async with api.sessions() as session:
        await session.execute(
            update(Resource).where(Resource.business_id == w.business_id).values(is_active=False)
        )
        await session.commit()
    no_resource = await lookup(api, w.owner, w.business_id)
    assert no_resource.status_code == 404
    assert await queue_rows(api, w.business_id) == 0


@pytest.mark.parametrize("status", ["paused", "closed"])
async def test_paused_and_closed_queues_are_returned_unchanged(api: Api, status: str) -> None:
    w = await world(api)
    first = await queue_id(w)
    async with api.sessions() as session:
        await session.execute(
            update(Queue).where(Queue.id == uuid.UUID(first)).values(status=status)
        )
        await session.commit()
    body = (await lookup(api, w.owner, w.business_id)).json()
    assert (body["id"], body["status"]) == (first, status)


async def test_two_businesses_never_share_a_queue(api: Api) -> None:
    first, second = await world(api), await world(api)
    assert await queue_id(first) != await queue_id(second)


@pytest.mark.parametrize(
    ("zone", "clock", "local_day"),
    [
        ("Asia/Kolkata", utc(20, day=1), "2026-11-02"),  # UTC still 1 Nov, local already 2 Nov
        ("America/New_York", utc(2, day=2), "2026-11-01"),  # UTC already 2 Nov, local 1 Nov
        ("Asia/Kolkata", utc(18, 29, day=1), "2026-11-01"),  # one minute before local midnight
    ],
)
async def test_queue_date_is_the_business_local_day(
    api: Api, zone: str, clock: datetime, local_day: str
) -> None:
    w = await world(api, zone=zone)
    api.clock.now = clock.timestamp()
    body = (await lookup(api, w.owner, w.business_id)).json()
    assert body["queue_date"] == local_day


async def test_a_new_local_day_creates_a_new_queue(api: Api) -> None:
    w = await world(api)
    api.clock.now = utc(18, 29, day=1).timestamp()  # 23:59 in Kolkata
    before = await queue_id(w)
    api.clock.now = utc(18, 30, day=1).timestamp()  # 00:00 in Kolkata
    after = await queue_id(w)
    assert before != after and await queue_rows(api, w.business_id) == 2


async def test_lookup_rate_limit(api: Api) -> None:
    w = await world(api)
    for _ in range(120):
        assert (await lookup(api, w.owner, w.business_id)).status_code == 200
    blocked = await lookup(api, w.owner, w.business_id)
    assert blocked.status_code == 429 and "Retry-After" in blocked.headers


# --- GET /queues/{id} ---


async def add_token(
    api: Api,
    w: World,
    queue: str,
    token_no: int,
    status: str,
    priority: int = 0,
    queued_at: datetime | None = None,
    **values: object,
) -> str:
    customer_id = values.pop("customer_id", None)
    person = await api.create_user(
        "customer",
        name=f"Customer {token_no}",
        phone=f"+91{uuid.uuid4().int % 10**10:010d}",
        email=new_email(),
        password_hash="argon2-hash-must-never-leak",
    )
    async with api.sessions() as session:
        token_id = await session.scalar(
            insert(QueueToken)
            .values(
                business_id=w.business_id,
                queue_id=uuid.UUID(queue),
                token_no=token_no,
                source="walk_in",
                customer_id=customer_id or person.id,
                status=status,
                priority=priority,
                queued_at=queued_at or utc(3, 30),
                **values,
            )
            .returning(QueueToken.id)
        )
        await session.commit()
    return str(token_id)


async def test_snapshot_order_positions_counts_and_privacy(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    ids = {
        "t1": await add_token(api, w, queue, 1, "waiting", 0, utc(3, 30)),
        "t2": await add_token(api, w, queue, 2, "waiting", 2, utc(4, 0)),
        "t3": await add_token(api, w, queue, 3, "waiting", 0, utc(2, 30)),
        "t4": await add_token(api, w, queue, 4, "called", 1, utc(1, 30), called_at=utc(5, 0)),
        "t5": await add_token(api, w, queue, 5, "in_service", 0, utc(0, 30), started_at=utc(5)),
        "t6": await add_token(api, w, queue, 6, "completed"),
        "t7": await add_token(api, w, queue, 7, "no_show"),
        "t8": await add_token(api, w, queue, 8, "cancelled"),
        "t9": await add_token(api, w, queue, 9, "cancelled"),
    }
    response = await snapshot(api, w.owner, queue)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == QUEUE_KEYS | {"counts", "tokens"}
    assert body["counts"] == {"completed": 1, "no_show": 1, "cancelled": 2}
    # priority DESC, then queued_at ASC
    expected = [("t2", 1), ("t4", None), ("t5", None), ("t3", 2), ("t1", 3)]
    assert [(t["id"], t["order_position"]) for t in body["tokens"]] == [
        (ids[name], position) for name, position in expected
    ]
    first = body["tokens"][0]
    assert set(first["customer"]) == {"id", "name"} and first["customer"]["name"] == "Customer 2"
    assert first["queued_at"].endswith("+05:30") and first["called_at"] is None
    assert body["tokens"][1]["called_at"].endswith("+05:30")
    raw = json.dumps(body)
    for forbidden in ("+91", "@example", "argon2", "phone", "email", "password"):
        assert forbidden not in raw


async def test_empty_queue_snapshot(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    body = (await snapshot(api, w.owner, queue)).json()
    assert body["tokens"] == []
    assert body["counts"] == {"completed": 0, "no_show": 0, "cancelled": 0}


async def test_snapshot_access_rules(api: Api) -> None:
    w = await world(api)
    other = await world(api)
    queue = await queue_id(w)
    staff = await w.staff()
    assert (await snapshot(api, w.owner, queue)).status_code == 200
    assert (await snapshot(api, staff, queue)).status_code == 200
    _, customer_headers = await customer(api)
    for headers in (customer_headers, await admin(api)):
        assert (await snapshot(api, headers, queue)).status_code == 403
        assert (await snapshot(api, headers, str(uuid.uuid4()))).status_code == 403  # before id
    assert (await api.client.get(f"/v1/queues/{queue}")).status_code == 401
    missing = await snapshot(api, w.owner, str(uuid.uuid4()))
    assert missing.status_code == 404
    for stranger in (other.owner, await other.staff()):
        response = await snapshot(api, stranger, queue)
        assert response.status_code == 404 and response.json() == missing.json()


async def test_removed_staff_gets_404_on_the_next_call(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    created = await new_staff(api, w.owner, w.business_id)
    saved = api.clock.now
    api.clock.now = datetime.now(UTC).timestamp()  # login stamps iat with the app clock
    staff = await login(api, created["email"], PASSWORD)
    api.clock.now = saved
    assert (await snapshot(api, staff, queue)).status_code == 200
    removed = await api.client.delete(
        f"/v1/businesses/{w.business_id}/staff/{created['id']}", headers=w.owner
    )
    assert removed.status_code == 204
    assert (await snapshot(api, staff, queue)).status_code == 404


async def test_snapshot_creates_nothing(api: Api) -> None:
    w = await world(api)
    assert (await snapshot(api, w.owner, str(uuid.uuid4()))).status_code == 404
    assert await queue_rows(api, w.business_id) == 0


async def test_snapshot_uses_three_statements_whatever_the_token_count(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    engine = api.app.state.engine.sync_engine
    counts = []
    statements: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    for batch in (1, 5):
        for number in range(batch):
            await add_token(api, w, queue, 100 * batch + number, "waiting")
        statements.clear()
        event.listen(engine, "before_cursor_execute", record)
        try:
            assert (await snapshot(api, w.owner, queue)).status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", record)
        counts.append(len(statements))
    assert counts == [3, 3]  # role check, queue+membership+counts, active tokens with customers
