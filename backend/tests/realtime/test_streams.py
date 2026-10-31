"""The three SSE streams end to end: real servers, the real relay, real Postgres and Redis."""

import asyncio
import contextlib
import json
import logging
import statistics
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import delete, select

from app.core.security import create_stream_token
from app.models import BusinessMember, OutboxEvent
from app.realtime import hub as hub_module
from app.realtime import sse
from app.realtime.hub import Hub
from tests.integration.api import Api
from tests.integration.test_booking import World, customer, world
from tests.integration.test_queues import queue_id
from tests.integration.test_serving import call_next, priority
from tests.integration.test_tokens import join
from tests.realtime.conftest import Stream, open_stream, relay, serve

pytestmark = pytest.mark.anyio


async def staff_token(api: Api, headers: dict[str, str], queue: str) -> str:
    response = await api.client.post(f"/v1/queues/{queue}/stream-token", headers=headers)
    assert response.status_code == 200, response.text
    return str(response.json()["stream_token"])


async def joined(api: Api, queue: str) -> tuple[dict[str, str], dict[str, Any]]:
    """A new customer joins; returns their headers and GET /tokens/{id} (with stream_token)."""
    user, headers = await customer(api)
    token = (await join(api, headers, queue)).json()
    detail = await api.client.get(f"/v1/tokens/{token['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    del user
    return headers, detail.json()


async def setup(api: Api) -> tuple[World, str]:
    w = await world(api)
    return w, await queue_id(w)


# --- staff stream ---


async def test_staff_stream_snapshot_then_deltas_with_headers(api: Api) -> None:
    w, queue = await setup(api)
    first, _ = await joined(api, queue)
    token = await staff_token(api, w.owner, queue)
    async with (
        relay(api),
        serve(api) as (base, _),
        httpx.AsyncClient(base_url=base, timeout=30) as http,
    ):
        async with open_stream(http, f"/v1/queues/{queue}/stream?stream_token={token}") as s:
            headers = s.response.headers
            assert headers["content-type"].startswith("text/event-stream")
            assert headers["cache-control"] == "no-cache"
            assert headers["x-accel-buffering"] == "no"
            assert headers["connection"] == "keep-alive"
            snap = await s.expect("snapshot")
            assert len(snap["data"]["tokens"]) == 1 and snap["id"] >= 1
            _, second = await joined(api, queue)
            event = await s.expect("token.joined")  # data is the event envelope
            assert event["id"] > snap["id"] and event["data"]["id"] == event["id"]
            assert event["data"]["data"]["token_id"] == second["id"]
            assert set(event["data"]["data"]["customer"]) == {"id", "name"}
            called = await call_next(api, w.owner, queue)
            delta = (await s.expect("token.called"))["data"]["data"]
            assert delta["token_id"] == called.json()["id"]
    del first


async def test_heartbeat(api: Api) -> None:
    w, queue = await setup(api)
    token = await staff_token(api, w.owner, queue)
    async with relay(api), serve(api, sse_heartbeat_sec=1, sse_max_stream_sec=30) as (base, _):
        async with httpx.AsyncClient(base_url=base, timeout=30) as http:
            async with open_stream(http, f"/v1/queues/{queue}/stream?stream_token={token}") as s:
                await s.expect("snapshot")
                assert (await s.next(within=5))["event"] == "heartbeat"


async def test_stream_token_rules(api: Api) -> None:
    w, queue = await setup(api)
    other = await world(api)
    other_queue = await queue_id(other)
    _, detail = await joined(api, queue)
    stranger, _ = await customer(api)
    settings = api.settings
    expired = create_stream_token(
        settings,
        "queue",
        uuid.UUID(queue),
        uuid.uuid4(),
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    staff = await staff_token(api, w.owner, queue)
    cases = {
        f"/v1/queues/{queue}/stream": 401,  # no token
        f"/v1/queues/{queue}/stream?stream_token={detail['stream_token']}": 401,  # wrong scope
        f"/v1/queues/{other_queue}/stream?stream_token={staff}": 401,  # another queue
        f"/v1/queues/{queue}/stream?stream_token={expired}": 401,
        f"/v1/tokens/{detail['id']}/stream?stream_token={staff}": 401,  # wrong scope
        f"/v1/tokens/{detail['id']}/stream?stream_token="
        + create_stream_token(settings, "token", uuid.UUID(detail["id"]), stranger.id): 404,
    }
    async with serve(api) as (base, app), httpx.AsyncClient(base_url=base, timeout=30) as http:
        for url, status in cases.items():
            assert (await http.get(url)).status_code == status, url
        assert app.state.hub.connections == 0


async def test_removed_staff_is_revoked(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sse, "RECHECK_SEC", 0.5)
    w, queue = await setup(api)
    created = (
        await api.client.post(
            f"/v1/businesses/{w.business_id}/staff",
            json={
                "name": "Sam",
                "email": f"{uuid.uuid4().hex[:8]}@example.test",
                "initial_password": "staff-password-1",
            },
            headers=w.owner,
        )
    ).json()
    staff_id = uuid.UUID(created["id"])
    token = create_stream_token(api.settings, "queue", uuid.UUID(queue), staff_id)
    async with serve(api) as (base, app), httpx.AsyncClient(base_url=base, timeout=30) as http:
        async with open_stream(http, f"/v1/queues/{queue}/stream?stream_token={token}") as s:
            await s.expect("snapshot")
            async with api.sessions() as session:
                await session.execute(
                    delete(BusinessMember).where(BusinessMember.user_id == staff_id)
                )
                await session.commit()
            revoked = await s.expect("revoked")
            assert revoked["data"] == {}
            with pytest.raises(EOFError):
                await s.next(within=5)
        for _ in range(1000):
            if app.state.hub.connections == 0:
                break
            await asyncio.sleep(0)
        assert app.state.hub.connections == 0


# --- customer stream ---


async def test_customer_stream_sends_only_their_own_changes(api: Api) -> None:
    w, queue = await setup(api)
    _, ahead = await joined(api, queue)
    me_headers, me = await joined(api, queue)
    async with (
        relay(api),
        serve(api) as (base, _),
        httpx.AsyncClient(base_url=base, timeout=30) as http,
    ):
        url = f"/v1/tokens/{me['id']}/stream?stream_token={me['stream_token']}"
        async with open_stream(http, url) as s:
            snap = await s.expect("snapshot")
            assert snap["data"] == {
                "token_id": me["id"],
                "token_no": me["token_no"],
                "status": "waiting",
                "ahead_count": 1,
                "next": False,
            }
            await joined(api, queue)  # someone BEHIND joins: nothing changes for me
            first_call = await call_next(api, w.owner, queue)  # the token ahead is called
            assert first_call.json()["id"] == ahead["id"]
            update = await s.expect("token.updated")
            assert update["data"]["ahead_count"] == 0 and update["data"]["next"] is True
            raised = await priority(api, w.owner, me["id"], 2, me["version"])  # staff-only fact
            assert raised.status_code == 200
            mine = await call_next(api, w.owner, queue)
            assert mine.json()["id"] == me["id"]
            called = await s.expect("token.updated")
            assert called["data"]["status"] == "called" and called["data"]["ahead_count"] is None
            for kind in ("priority", "name", "phone", "email", ahead["id"]):
                assert kind not in s.raw, kind
    del me_headers


# --- board ---


async def test_board_carries_token_numbers_only(api: Api) -> None:
    w, queue = await setup(api)
    for _ in range(3):
        await joined(api, queue)
    async with (
        relay(api),
        serve(api) as (base, _),
        httpx.AsyncClient(base_url=base, timeout=30) as http,
    ):
        async with open_stream(http, f"/v1/businesses/{w.business_id}/board") as s:
            snap = await s.expect("snapshot")
            assert snap["data"] == {"now_serving": [], "next": [1, 2, 3]}
            await call_next(api, w.owner, queue)
            board = await s.expect("board.updated")
            assert board["data"] == {"now_serving": [1], "next": [2, 3]}
            assert set(json.dumps(board).replace('"', "")) <= set(
                "{}[],:0123456789 abcdefghijklmnopqrstuvwxyz_."
            )
            for forbidden in ("customer", "name", "phone", "email", "token_id", "priority"):
                assert forbidden not in s.raw.replace("board.updated", ""), forbidden


# --- replay, reset, duplicates ---


async def collect(stream: Stream, count: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while len(out) < count:
        event = await stream.next()
        if event["event"] != "heartbeat":
            out.append(event)
    return out


async def test_reconnect_replays_exactly_the_missed_events(api: Api) -> None:
    w, queue = await setup(api)
    token = await staff_token(api, w.owner, queue)
    url = f"/v1/queues/{queue}/stream?stream_token={token}"
    async with (
        relay(api),
        serve(api) as (base, _),
        httpx.AsyncClient(base_url=base, timeout=30) as http,
    ):
        async with open_stream(http, url) as s:
            await s.expect("snapshot")
            await joined(api, queue)
            last_seen = (await s.expect("token.joined"))["id"]
        for _ in range(3):  # missed while disconnected
            await joined(api, queue)
        await call_next(api, w.owner, queue)
        async with api.sessions() as session:
            missed = list(
                (
                    await session.scalars(
                        select(OutboxEvent.id)
                        .where(OutboxEvent.queue_id == uuid.UUID(queue), OutboxEvent.id > last_seen)
                        .order_by(OutboxEvent.id)
                    )
                ).all()
            )
        assert len(missed) == 4
        async with open_stream(http, url, **{"Last-Event-ID": str(last_seen)}) as s:
            replayed = await collect(s, 4)
            assert [e["id"] for e in replayed] == missed  # in order, nothing missing
            assert [e["event"] for e in replayed] == ["token.joined"] * 3 + ["token.called"]
            await joined(api, queue)  # and then live, with no duplicate
            live = await s.expect("token.joined")
            assert live["id"] > missed[-1]


@pytest.mark.parametrize(
    ("last", "reason"), [("999999999", "unknown"), ("abc", "unknown"), ("old", "expired")]
)
async def test_reset_then_snapshot(api: Api, last: str, reason: str) -> None:
    w, queue = await setup(api)
    await joined(api, queue)
    if last == "old":  # the event was cleaned up (older than retention)
        async with api.sessions() as session:
            first = await session.scalar(select(OutboxEvent.id).order_by(OutboxEvent.id))
            await session.execute(delete(OutboxEvent).where(OutboxEvent.id == first))
            await session.commit()
        await joined(api, queue)
        last = str(first)
    token = await staff_token(api, w.owner, queue)
    async with serve(api) as (base, _), httpx.AsyncClient(base_url=base, timeout=30) as http:
        url = f"/v1/queues/{queue}/stream?stream_token={token}"
        async with open_stream(http, url, **{"Last-Event-ID": last}) as s:
            assert (await s.expect("reset"))["data"] == {"reason": reason}
            await s.expect("snapshot")


async def test_a_republished_event_is_delivered_once(api: Api) -> None:
    w, queue = await setup(api)
    token = await staff_token(api, w.owner, queue)
    async with (
        relay(api),
        serve(api) as (base, _),
        httpx.AsyncClient(base_url=base, timeout=30) as http,
    ):
        async with open_stream(http, f"/v1/queues/{queue}/stream?stream_token={token}") as s:
            await s.expect("snapshot")
            await joined(api, queue)
            event = await s.expect("token.joined")
            redis = Redis.from_url(api.settings.redis_url)
            envelope = {
                "id": event["id"],
                "type": "token.joined",
                "queue_id": queue,
                "occurred_at": "",
                "data": event["data"],
            }
            await redis.publish(f"queue:{queue}", json.dumps(envelope))  # a relay crash replays it
            await redis.aclose()
            await joined(api, queue)
            following = await s.expect("token.joined")
            assert following["id"] > event["id"]  # the duplicate never came through


# --- two instances, limits, slow consumers, reconnect storms ---


async def test_two_instances_both_deliver(api: Api) -> None:
    w, queue = await setup(api)
    token = await staff_token(api, w.owner, queue)
    async with relay(api), serve(api) as (base_a, _), serve(api) as (base_b, _):
        async with httpx.AsyncClient(timeout=30) as http:
            async with (
                open_stream(http, f"{base_a}/v1/queues/{queue}/stream?stream_token={token}") as a,
                open_stream(http, f"{base_b}/v1/queues/{queue}/stream?stream_token={token}") as b,
            ):
                await a.expect("snapshot")
                await b.expect("snapshot")
                await joined(api, queue)
                assert (await a.expect("token.joined"))["id"] == (await b.expect("token.joined"))[
                    "id"
                ]


async def test_connection_caps_and_counters_return_to_zero(api: Api) -> None:
    w, queue = await setup(api)
    token = await staff_token(api, w.owner, queue)
    url = f"/v1/queues/{queue}/stream?stream_token={token}"
    async with (
        serve(api, sse_max_conn_per_user=2) as (base, app),
        httpx.AsyncClient(base_url=base, timeout=30) as http,
    ):
        hub: Hub = app.state.hub
        async with open_stream(http, url) as one, open_stream(http, url) as two:
            await one.expect("snapshot")
            await two.expect("snapshot")
            third = await http.get(url)
            assert third.status_code == 429 and "Retry-After" in third.headers
            assert hub.connections == 2
        for _ in range(100000):
            if hub.connections == 0:
                break
            await asyncio.sleep(0)
        assert hub.connections == 0  # released on every path


async def test_a_slow_consumer_is_dropped(api: Api) -> None:
    hub = Hub(api.settings.redis_url, max_per_user=5, max_total=10)
    await hub.start()
    redis = Redis.from_url(api.settings.redis_url)
    try:
        client = hub.admit("slow")
        await hub.join(client, "queue:slow")
        for number in range(hub_module.CLIENT_QUEUE_SIZE + 5):
            await redis.publish("queue:slow", json.dumps({"id": number + 1}))
        for _ in range(100000):
            if client.overflow:
                break
            await asyncio.sleep(0)
        assert client.overflow and client.events.qsize() == hub_module.CLIENT_QUEUE_SIZE
        await hub.release(client)
        assert hub.connections == 0
    finally:
        await redis.aclose()
        await hub.close()


async def test_board_connections_are_rate_limited_per_instance(api: Api) -> None:
    hub = Hub(api.settings.redis_url, max_per_user=5, max_total=1000)
    clients = [hub.admit(None, board=True) for _ in range(hub_module.BOARD_CONNECTS_PER_MINUTE)]
    with pytest.raises(Exception) as error:
        hub.admit(None, board=True)
    assert getattr(error.value, "code", "") == "RATE_LIMITED"
    for client in clients:
        await hub.release(client)
    assert hub.connections == 0
    await hub.close()


async def test_a_reconnect_storm_succeeds_without_exhausting_the_pool(api: Api) -> None:
    w, queue = await setup(api)
    for _ in range(5):
        await joined(api, queue)
    token = await staff_token(api, w.owner, queue)
    async with serve(api, sse_max_conn_per_user=100, db_pool_size=5) as (base, app):
        async with httpx.AsyncClient(base_url=base, timeout=60) as http:

            async def one() -> str:
                url = f"/v1/queues/{queue}/stream?stream_token={token}"
                async with open_stream(http, url) as s:
                    return str((await s.expect("snapshot"))["event"])

            results = await asyncio.gather(*(one() for _ in range(40)))
        assert results == ["snapshot"] * 40
        for _ in range(100000):
            if app.state.hub.connections == 0:
                break
            await asyncio.sleep(0)
        assert app.state.hub.connections == 0


# --- GET /tokens/{id} ---


async def test_get_token_with_stream_token(api: Api) -> None:
    w, queue = await setup(api)
    headers, detail = await joined(api, queue)
    assert set(detail) == {
        "id",
        "queue_id",
        "token_no",
        "status",
        "source",
        "queued_at",
        "joined_at",
        "version",
        "ahead_count",
        "stream_token",
        "expires_in",
    }
    assert detail["expires_in"] == api.settings.stream_token_ttl_sec
    _, stranger = await customer(api)
    missing = await api.client.get(f"/v1/tokens/{uuid.uuid4()}", headers=stranger)
    theirs = await api.client.get(f"/v1/tokens/{detail['id']}", headers=stranger)
    assert missing.status_code == theirs.status_code == 404 and missing.json() == theirs.json()
    assert (await api.client.get(f"/v1/tokens/{detail['id']}", headers=w.owner)).status_code == 403
    del headers


# --- latency (NFR-1) ---


async def received(stream: Stream, acted: float) -> float:
    await stream.expect("token.updated")
    return time.perf_counter() - acted


async def test_latency_from_staff_action_to_customer_event(
    api: Api, capsys: pytest.CaptureFixture[str]
) -> None:
    """50 customer streams; each call-next changes every remaining customer's view. The delay is
    from the staff action's response to each customer's event."""
    w, queue = await setup(api)
    people = [await joined(api, queue) for _ in range(50)]
    delays: list[float] = []
    async with relay(api), serve(api, sse_max_conn_per_user=100) as (base, _):
        limits = httpx.Limits(max_connections=200)
        async with httpx.AsyncClient(base_url=base, timeout=60, limits=limits) as http:
            async with contextlib.AsyncExitStack() as stack:
                streams = []
                for _, detail in people:
                    url = f"/v1/tokens/{detail['id']}/stream?stream_token={detail['stream_token']}"
                    stream = await stack.enter_async_context(open_stream(http, url))
                    await stream.expect("snapshot")
                    streams.append(stream)
                for call in range(5):  # call i: customers i..49 all see a change
                    response = await call_next(api, w.owner, queue)
                    acted = time.perf_counter()
                    assert response.status_code == 200

                    delays += await asyncio.gather(*(received(s, acted) for s in streams[call:]))
    delays.sort()
    p50, p95 = statistics.median(delays), delays[max(0, int(len(delays) * 0.95) - 1)]
    with capsys.disabled():
        print(
            f"\nlatency, 50 customer streams, 5 call-next ({len(delays)} events): "
            f"p50={p50 * 1000:.0f} ms p95={p95 * 1000:.0f} ms"
        )
    assert p95 < 1.0


# --- lifetimes, Pub/Sub loss, log redaction, relay health ---


async def test_every_stream_ends_at_its_lifetime_and_the_board_at_local_midnight(
    api: Api,
) -> None:
    w, queue = await setup(api)
    token = await staff_token(api, w.owner, queue)
    async with serve(api, sse_heartbeat_sec=1, sse_max_stream_sec=2) as (base, _):
        async with httpx.AsyncClient(base_url=base, timeout=30) as http:
            async with open_stream(http, f"/v1/queues/{queue}/stream?stream_token={token}") as s:
                await s.expect("snapshot")
                with pytest.raises(EOFError):
                    while True:
                        await s.next(within=10)
    # The board: two seconds before local midnight in Kolkata, with a long lifetime.
    api.clock.now = datetime(2026, 11, 1, 18, 29, 58, tzinfo=UTC).timestamp()
    async with serve(api) as (base, _), httpx.AsyncClient(base_url=base, timeout=30) as http:
        started = time.perf_counter()
        async with open_stream(http, f"/v1/businesses/{w.business_id}/board") as s:
            await s.expect("snapshot")
            with pytest.raises(EOFError):
                while True:
                    await s.next(within=10)
        assert time.perf_counter() - started < 8


async def test_a_dropped_pubsub_connection_loses_no_event(api: Api) -> None:
    w, queue = await setup(api)
    token = await staff_token(api, w.owner, queue)
    async with (
        relay(api),
        serve(api) as (base, _),
        httpx.AsyncClient(base_url=base, timeout=30) as http,
    ):
        async with open_stream(http, f"/v1/queues/{queue}/stream?stream_token={token}") as s:
            await s.expect("snapshot")
            redis = Redis.from_url(api.settings.redis_url)
            await redis.execute_command("CLIENT", "KILL", "TYPE", "pubsub")  # the hub's subscriber
            await redis.aclose()
            await joined(api, queue)  # published while the hub reconnects
            event = await s.expect("token.joined", within=20)
            assert event["data"]["type"] == "token.joined"


def test_stream_tokens_are_redacted_from_the_access_log() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", "/v1/queues/q/stream?stream_token=abc.def&x=1", "1.1", 200),
        None,
    )
    assert sse.RedactStreamToken().filter(record)
    assert "abc.def" not in record.getMessage()
    assert "stream_token=[redacted]&x=1" in record.getMessage()


async def test_an_event_between_the_first_read_and_live_delivery_is_not_lost(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Right after the stream's first database read, an event is made AND published. The stream
    subscribed before that read, so it must still receive the event live."""
    from app.realtime import sse

    w, queue = await setup(api)
    token = await staff_token(api, w.owner, queue)
    real = sse.first_frames

    async def gap(*args: Any, **kwargs: Any) -> Any:  # Any: passes the arguments through
        frames = await real(*args, **kwargs)
        await joined(api, queue)
        for _ in range(100000):  # until the relay has published it
            async with api.sessions() as session:
                pending = await session.scalar(
                    select(OutboxEvent.id).where(OutboxEvent.published_at.is_(None))
                )
            if pending is None:
                break
            await asyncio.sleep(0)
        return frames

    monkeypatch.setattr("app.api.v1.streams.first_frames", gap)
    async with (
        relay(api),
        serve(api) as (base, _),
        httpx.AsyncClient(base_url=base, timeout=30) as http,
    ):
        async with open_stream(http, f"/v1/queues/{queue}/stream?stream_token={token}") as s:
            await s.expect("snapshot")
            event = await s.expect("token.joined", within=10)
            assert event["data"]["type"] == "token.joined"
