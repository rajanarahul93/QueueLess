"""The staff view of a token carries its service (B5): the appointment's, or the one chosen at
Start. Null means unknown, never hidden. The customer side gains nothing."""

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.dialects import postgresql

from app.models import OutboxEvent, Service
from app.realtime.outbox import emit_token_event
from app.services.queue import active_tokens_query
from tests.integration.api import Api
from tests.integration.test_availability import utc
from tests.integration.test_booking import World, customer, world
from tests.integration.test_queues import add_token, queue_id, snapshot
from tests.integration.test_serving import act, appointment_token, call_next, priority
from tests.integration.test_tokens import join, token_row

pytestmark = pytest.mark.anyio

SERVICE_KEYS = ("service_id", "service_name")


def keys_at_any_depth(node: Any, found: list[str] | None = None) -> list[str]:
    collected = [] if found is None else found
    if isinstance(node, dict):
        for key, value in node.items():
            collected.append(key)
            keys_at_any_depth(value, collected)
    elif isinstance(node, list):
        for item in node:
            keys_at_any_depth(item, collected)
    return collected


async def service_name(api: Api, service_id: str) -> str:
    async with api.sessions() as session:
        query = select(Service.name).where(Service.id == uuid.UUID(service_id))
        found = await session.scalar(query)
        return str(found)


async def staff_tokens(api: Api, w: World, queue: str) -> dict[str, dict[str, Any]]:
    body = (await snapshot(api, w.owner, queue)).json()
    return {token["id"]: token for token in body["tokens"]}


async def token_events(api: Api, token_id: str) -> list[OutboxEvent]:
    async with api.sessions() as session:
        rows = await session.execute(
            select(OutboxEvent)
            .where(OutboxEvent.event_type.startswith("token."))
            .order_by(OutboxEvent.id)
        )
        return [row for row in rows.scalars() if row.payload.get("token_id") == token_id]


async def test_an_appointment_token_carries_its_service_and_a_walk_in_carries_null(
    api: Api,
) -> None:
    w = await world(api)
    _, _, checked_in = await appointment_token(w)
    queue = str((await token_row(api, checked_in)).queue_id)
    _, walker = await customer(api)
    walked = (await join(api, walker, queue)).json()["id"]
    tokens = await staff_tokens(api, w, queue)
    assert tokens[checked_in]["service_id"] == w.service_id
    assert tokens[checked_in]["service_name"] == await service_name(api, w.service_id)
    # A walk-in has no service until staff choose one at Start: unknown, not hidden.
    assert tokens[walked]["service_id"] is None and tokens[walked]["service_name"] is None


async def test_a_walk_in_start_puts_its_service_on_the_token_everywhere(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    staff = await w.staff()
    _, walker = await customer(api)
    walked = (await join(api, walker, queue)).json()
    called = (await call_next(api, staff, queue)).json()
    assert called["id"] == walked["id"] and called["service_id"] is None
    started = await act(
        api, staff, walked["id"], "start", called["version"], service_id=w.service_id
    )
    assert started.status_code == 200, started.text
    expected = await service_name(api, w.service_id)
    # The action response, the next snapshot and the token.started event all agree.
    body = started.json()
    assert (body["service_id"], body["service_name"]) == (w.service_id, expected)
    tokens = await staff_tokens(api, w, queue)
    assert tokens[walked["id"]]["service_id"] == w.service_id
    assert tokens[walked["id"]]["service_name"] == expected
    events = await token_events(api, walked["id"])
    [event_row] = [e for e in events if e.event_type == "token.started"]
    assert event_row.payload["service_id"] == w.service_id
    assert event_row.payload["service_name"] == expected


async def test_every_token_event_payload_carries_the_service_fields(api: Api) -> None:
    w = await world(api)
    staff = await w.staff()
    _, _, checked_in = await appointment_token(w)  # token.joined with a service
    queue = str((await token_row(api, checked_in)).queue_id)
    _, walker = await customer(api)
    walked = (await join(api, walker, queue)).json()  # token.joined with none
    await priority(api, staff, walked["id"], 1, walked["version"])  # token.priority_changed
    called = (await call_next(api, staff, queue)).json()  # token.called
    await act(api, staff, called["id"], "skip", called["version"])  # token.skipped
    again = (await call_next(api, staff, queue)).json()
    await act(api, staff, again["id"], "start", again["version"], service_id=w.service_id)
    await act(api, staff, again["id"], "complete", again["version"] + 1)  # started, completed
    last = (await call_next(api, staff, queue)).json()
    await act(api, staff, last["id"], "no-show", last["version"])  # token.no_show
    _, leaver = await customer(api)
    left = (await join(api, leaver, queue)).json()
    await api.client.delete(f"/v1/tokens/{left['id']}", headers=leaver)  # token.cancelled
    async with api.sessions() as session:
        rows = (
            await session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type.startswith("token."),
                    OutboxEvent.queue_id == uuid.UUID(queue),  # the module shares one schema
                )
                .order_by(OutboxEvent.id)
            )
        ).all()
    assert {row.event_type for row in rows} == {
        "token.joined",
        "token.priority_changed",
        "token.called",
        "token.skipped",
        "token.started",
        "token.completed",
        "token.no_show",
        "token.cancelled",
    }
    expected = await service_name(api, w.service_id)
    for row in rows:
        assert set(SERVICE_KEYS) <= set(row.payload), row.event_type
        service_id, name = row.payload["service_id"], row.payload["service_name"]
        assert (service_id is None) == (name is None), row.event_type  # both, or neither
        assert service_id in (None, w.service_id) and name in (None, expected), row.event_type
    # The walk-in's own events: none before Start carries a service, every one after does.
    walk_in = [row for row in rows if row.payload["token_id"] == again["id"]]
    assert [row.payload["service_id"] is not None for row in walk_in] == [
        row.event_type in ("token.started", "token.completed") for row in walk_in
    ], [row.event_type for row in walk_in]


async def test_a_service_rename_shows_in_the_next_snapshot(api: Api) -> None:
    w = await world(api)
    _, _, checked_in = await appointment_token(w)
    queue = str((await token_row(api, checked_in)).queue_id)
    before = (await staff_tokens(api, w, queue))[checked_in]["service_name"]
    async with api.sessions() as session:
        await session.execute(
            update(Service).where(Service.id == uuid.UUID(w.service_id)).values(name="Renamed")
        )
        await session.commit()
    after = (await staff_tokens(api, w, queue))[checked_in]["service_name"]
    # The snapshot resolves the name on every read, so a rename needs no event.
    assert (before, after) == ("Checkup", "Renamed")


async def test_the_snapshot_statement_count_does_not_grow_with_the_number_of_tokens(
    api: Api,
) -> None:
    w = await world(api)
    queue = await queue_id(w)

    async def count_statements(tokens: int) -> int:
        for number in range(tokens):
            await add_token(api, w, queue, number + 1, "waiting", 0, utc(3))
        statements = 0

        def seen(*_: object, **__: object) -> None:
            nonlocal statements
            statements += 1

        engine = api.app.state.engine.sync_engine
        event.listen(engine, "before_cursor_execute", seen)
        try:
            body = (await snapshot(api, w.owner, queue)).json()
        finally:
            event.remove(engine, "before_cursor_execute", seen)
        assert len(body["tokens"]) == tokens
        return statements

    few = await count_statements(5)
    async with api.sessions() as session:  # start over in the same queue
        await session.execute(text("DELETE FROM queue_tokens WHERE queue_id = :q"), {"q": queue})
        await session.commit()
    many = await count_statements(50)
    assert few == many, (few, many)


async def test_staff_payloads_have_no_personal_keys_at_any_depth(api: Api) -> None:
    w = await world(api)
    staff = await w.staff()
    _, _, checked_in = await appointment_token(w)
    queue = str((await token_row(api, checked_in)).queue_id)
    called = (await call_next(api, staff, queue)).json()
    bodies = [(await snapshot(api, w.owner, queue)).json(), called]
    for body in bodies:
        for key in keys_at_any_depth(body):
            assert "phone" not in key and "email" not in key and "password" not in key, key
            assert key not in ("phone_verified_at", "password_hash", "no_show_count"), key
        raw = json.dumps(body)
        for forbidden in ("+91", "@example", "argon2"):
            assert forbidden not in raw, forbidden


async def test_the_customer_token_response_has_no_service_keys(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    _, headers = await customer(api)
    joined = (await join(api, headers, queue)).json()
    detail = (await api.client.get(f"/v1/tokens/{joined['id']}", headers=headers)).json()
    listed = (await api.client.get("/v1/tokens", params={"active": "true"}, headers=headers)).json()
    for body in (joined, detail, *listed):
        assert not set(SERVICE_KEYS) & set(keys_at_any_depth(body)), body


async def test_the_snapshot_join_does_not_scan_the_sessions_table(api: Api) -> None:
    """The real shape: ten active tokens, and a sessions table that keeps growing (one row per
    served token, never pruned). Without the index the join reads the whole table on every
    staff read; at this size the planner must reach for the index instead."""
    w = await world(api)
    queue = uuid.UUID(await queue_id(w))
    ids: dict[str, Any] = {
        "b": w.business_id,
        "r": w.resource,
        "q": queue,
        "s": uuid.UUID(w.service_id),
    }
    async with api.sessions() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, name, role, phone) SELECT gen_random_uuid(), 'u', "
                "'customer', '+7' || lpad(g::text, 11, '0') FROM generate_series(1, 20010) g"
            )
        )
        await session.execute(
            text(
                "WITH u AS (SELECT id, row_number() OVER (ORDER BY phone) AS n FROM users "
                "WHERE phone LIKE '+7%') "
                "INSERT INTO queue_tokens (business_id, queue_id, token_no, source, customer_id, "
                "status, queued_at) SELECT :b, :q, u.n, 'walk_in', u.id, "
                "CASE WHEN u.n <= 20000 THEN 'completed' ELSE 'waiting' END, now() FROM u"
            ),
            ids,
        )
        await session.execute(
            text(
                "INSERT INTO service_sessions (business_id, token_id, service_id, resource_id, "
                "started_at, ended_at, duration_sec) SELECT :b, t.id, :s, :r, now(), now(), 600 "
                "FROM queue_tokens t WHERE t.queue_id = :q AND t.status = 'completed'"
            ),
            ids,
        )
        await session.commit()
        for table in ("users", "queue_tokens", "service_sessions"):
            await session.execute(text(f"ANALYZE {table}"))
        rows = await session.scalar(text("SELECT count(*) FROM service_sessions"))
        compiled = active_tokens_query(queue).compile(
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
        explained = await session.execute(text(f"EXPLAIN {compiled}"))
        plan = "\n".join(row[0] for row in explained)
    assert rows is not None and rows >= 20000, rows
    print(f"\nservice_sessions rows={rows}\n{plan}")
    assert "Seq Scan on service_sessions" not in plan, plan
    assert "ix_service_sessions_token_id" in plan, plan


async def test_completing_a_token_finds_its_open_session_by_index(api: Api) -> None:
    """The Complete path reads the open session by token_id; same index, no scan."""
    w = await world(api)
    queue = await queue_id(w)
    staff = await w.staff()
    _, walker = await customer(api)
    walked = (await join(api, walker, queue)).json()
    called = (await call_next(api, staff, queue)).json()
    await act(api, staff, walked["id"], "start", called["version"], service_id=w.service_id)
    async with api.sessions() as session:
        await session.execute(text("ANALYZE service_sessions"))
        explained = await session.execute(
            text(
                "EXPLAIN SELECT started_at FROM service_sessions "
                f"WHERE token_id = '{walked['id']}' AND ended_at IS NULL"
            )
        )
        plan = "\n".join(row[0] for row in explained)
    print(f"\n{plan}")
    assert "ix_service_sessions_token_id" in plan, plan


def captured_sql(api: Api) -> tuple[list[str], Any]:
    """Every statement the engine runs, until the returned stop() is called."""
    statements: list[str] = []
    engine = api.app.state.engine.sync_engine

    def seen(_c: Any, _cu: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", seen)
    return statements, lambda: event.remove(engine, "before_cursor_execute", seen)


async def test_a_walk_in_join_emits_without_touching_the_service_tables(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hot path: the join emits its event inside the queue row lock, so it must not pay for
    a service a walk-in cannot have yet. (The ETA reads those tables either way; this watches
    the emit step alone.)"""
    w = await world(api)
    queue = await queue_id(w)
    _, headers = await customer(api)
    during: list[str] = []

    async def recording(*args: Any, **kwargs: Any) -> None:
        statements, stop = captured_sql(api)
        try:
            await emit_token_event(*args, **kwargs)
        finally:
            stop()
            during.extend(statements)

    monkeypatch.setattr("app.services.queue.emit_token_event", recording)
    response = await join(api, headers, queue)
    assert response.status_code == 201, response.text
    touched = [s for s in during if "service_sessions" in s or " services " in s]
    assert touched == [], touched
    assert len(during) == 3, during  # the advisory lock, the token read, the outbox insert
    # The event is there, with the service left unknown.
    [joined_event] = await token_events(api, response.json()["id"])
    assert joined_event.event_type == "token.joined"
    assert joined_event.payload["service_id"] is None
    assert joined_event.payload["service_name"] is None


async def test_a_walk_ins_start_and_complete_deltas_carry_the_service_the_rest_null(
    api: Api,
) -> None:
    w = await world(api)
    queue = await queue_id(w)
    staff = await w.staff()
    expected = await service_name(api, w.service_id)
    # One walk-in that is called, skipped, called again, started and completed.
    _, first = await customer(api)
    served = (await join(api, first, queue)).json()
    called = (await call_next(api, staff, queue)).json()
    await act(api, staff, served["id"], "skip", called["version"])
    recalled = (await call_next(api, staff, queue)).json()
    await act(api, staff, served["id"], "start", recalled["version"], service_id=w.service_id)
    started_version = (await staff_tokens(api, w, queue))[served["id"]]["version"]
    await act(api, staff, served["id"], "complete", started_version)
    by_type = {e.event_type: e.payload for e in await token_events(api, served["id"])}
    for event_type in ("token.started", "token.completed"):
        assert by_type[event_type]["service_id"] == w.service_id, event_type
        assert by_type[event_type]["service_name"] == expected, event_type
    for event_type in ("token.joined", "token.called", "token.skipped"):
        assert by_type[event_type]["service_id"] is None, event_type
        assert by_type[event_type]["service_name"] is None, event_type
    # A second walk-in that is cancelled, and a third marked no-show: never started, so null.
    _, second = await customer(api)
    left = (await join(api, second, queue)).json()
    await api.client.delete(f"/v1/tokens/{left['id']}", headers=second)
    _, third = await customer(api)
    missing = (await join(api, third, queue)).json()
    no_show_call = (await call_next(api, staff, queue)).json()
    assert no_show_call["id"] == missing["id"]
    await act(api, staff, missing["id"], "no-show", no_show_call["version"])
    for token_id, event_type in ((left["id"], "token.cancelled"), (missing["id"], "token.no_show")):
        [row] = [e for e in await token_events(api, token_id) if e.event_type == event_type]
        assert row.payload["service_id"] is None, event_type
        assert row.payload["service_name"] is None, event_type


async def test_an_appointment_check_in_event_carries_its_service(api: Api) -> None:
    w = await world(api)
    _, _, checked_in = await appointment_token(w)
    [joined_event] = await token_events(api, checked_in)
    assert joined_event.event_type == "token.joined"
    assert joined_event.payload["service_id"] == w.service_id
    assert joined_event.payload["service_name"] == await service_name(api, w.service_id)
