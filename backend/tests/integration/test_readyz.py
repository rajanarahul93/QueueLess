"""GET /readyz: Postgres, Redis and the worker relay, each with its own short timeout."""

import asyncio
import secrets
import time
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from app.core import readiness
from app.db.session import create_engine
from app.realtime.relay import HEALTH_KEY
from app.workers.health import RELAY_STALE_SEC as STALE_AFTER_SEC
from tests.integration.api import Api

pytestmark = pytest.mark.anyio

ALL_OK = {"status": "ok", "checks": {"postgres": "ok", "redis": "ok", "relay": "ok"}}


async def relay_ran(api: Api, ago: float = 0.0) -> None:
    await api.redis.set(HEALTH_KEY, str(api.clock() - ago))


@pytest.fixture
async def silent_port() -> AsyncIterator[int]:
    """A TCP port that accepts connections and never answers, like a hung dependency."""
    held: list[asyncio.StreamWriter] = []

    async def hold(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        held.append(writer)

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    yield int(server.sockets[0].getsockname()[1])
    for writer in held:
        writer.close()
    server.close()
    await server.wait_closed()


async def test_everything_up_is_200_with_per_check_ok(api: Api) -> None:
    await relay_ran(api)
    response = await api.client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == ALL_OK


async def test_a_relay_that_never_ran_is_a_503_naming_the_relay(api: Api) -> None:
    await api.redis.delete(HEALTH_KEY)
    response = await api.client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"postgres": "ok", "redis": "ok", "relay": "failed"},
    }


async def test_a_stale_relay_is_a_503(api: Api) -> None:
    too_old = STALE_AFTER_SEC + 5
    still_fresh = STALE_AFTER_SEC - 5
    await relay_ran(api, ago=too_old)
    assert (await api.client.get("/readyz")).json()["checks"]["relay"] == "failed"
    await relay_ran(api, ago=still_fresh)
    assert (await api.client.get("/readyz")).status_code == 200


async def test_redis_down_fails_redis_and_the_relay_only(api: Api) -> None:
    await relay_ran(api)
    healthy = api.app.state.redis
    api.app.state.redis = Redis.from_url("redis://127.0.0.1:1/0")
    try:
        response = await api.client.get("/readyz")
    finally:
        await api.app.state.redis.aclose()
        api.app.state.redis = healthy
    assert response.status_code == 503
    assert response.json()["checks"] == {"postgres": "ok", "redis": "failed", "relay": "failed"}


async def test_postgres_down_fails_postgres_only(api: Api) -> None:
    await relay_ran(api)
    healthy = api.app.state.engine
    api.app.state.engine = create_engine("postgresql+asyncpg://u:p@127.0.0.1:1/d")
    try:
        response = await api.client.get("/readyz")
    finally:
        await api.app.state.engine.dispose()
        api.app.state.engine = healthy
    assert response.status_code == 503
    assert response.json()["checks"] == {"postgres": "failed", "redis": "ok", "relay": "ok"}


async def test_a_hung_dependency_times_out_instead_of_hanging(
    api: Api, silent_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readiness, "CHECK_TIMEOUT_SEC", 0.3)
    healthy_redis, healthy_engine = api.app.state.redis, api.app.state.engine
    api.app.state.redis = Redis.from_url(f"redis://127.0.0.1:{silent_port}/0")
    api.app.state.engine = create_engine(f"postgresql+asyncpg://u:p@127.0.0.1:{silent_port}/d")
    started = time.monotonic()
    try:
        response = await api.client.get("/readyz")
    finally:
        await api.app.state.redis.aclose()
        await api.app.state.engine.dispose()
        api.app.state.redis, api.app.state.engine = healthy_redis, healthy_engine
    assert time.monotonic() - started < 3  # the checks run side by side, each capped
    assert response.status_code == 503
    assert response.json()["checks"] == {"postgres": "failed", "redis": "failed", "relay": "failed"}


async def test_the_body_never_names_hosts_urls_or_secrets(api: Api) -> None:
    healthy = api.app.state.redis
    marker = secrets.token_hex(8)
    api.app.state.redis = Redis.from_url(f"redis://:{marker}@127.0.0.1:1/0")
    try:
        response = await api.client.get("/readyz")
    finally:
        await api.app.state.redis.aclose()
        api.app.state.redis = healthy
    text = response.text
    for forbidden in ("redis://", "postgresql", "127.0.0.1", marker, api.settings.jwt_secret):
        assert forbidden not in text
    assert set(response.json()) == {"status", "checks"}


async def test_it_is_not_rate_limited_and_leaves_no_limiter_state(api: Api) -> None:
    await relay_ran(api)
    for _ in range(60):
        assert (await api.client.get("/readyz")).status_code == 200
    assert [key async for key in api.redis.scan_iter("rl:*")] == []


async def test_healthz_stays_liveness_only_with_redis_down(api: Api) -> None:
    healthy = api.app.state.redis
    api.app.state.redis = Redis.from_url("redis://127.0.0.1:1/0")
    try:
        response = await api.client.get("/healthz")
    finally:
        await api.app.state.redis.aclose()
        api.app.state.redis = healthy
    assert response.status_code == 200 and response.json() == {"status": "ok"}


async def test_the_openapi_document_lists_readyz(api: Api) -> None:
    document = api.app.openapi()
    assert set(document["paths"]["/readyz"]["get"]["responses"]) >= {"200", "503"}
