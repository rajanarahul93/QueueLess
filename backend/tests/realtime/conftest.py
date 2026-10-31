"""Real HTTP for streams: httpx's in-process transport waits for the whole response, so streams
are served by real uvicorn servers on free ports in this test process, with the outbox relay
running. Every test gets a fresh database."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import uvicorn
from alembic import command
from redis.asyncio import Redis

from app.main import create_app
from app.realtime.relay import run_relay
from tests.integration.api import Api, open_api
from tests.integration.db import alembic_config, reset_schema


@pytest.fixture
def migrated_url(postgres_url: str) -> str:
    reset_schema(postgres_url)
    command.upgrade(alembic_config(postgres_url), "head")
    return postgres_url


@pytest.fixture
async def api(migrated_url: str, redis_url: str) -> AsyncIterator[Api]:
    overrides: dict[str, object] = {"db_pool_size": 30, "join_rate_limit_per_hour": 1000}
    async with open_api(migrated_url, redis_url, overrides) as opened:
        yield opened


@asynccontextmanager
async def serve(api: Api, **overrides: object) -> AsyncIterator[tuple[str, Any]]:
    """A uvicorn server running a fresh app on the shared database, Redis and clock."""
    settings = api.settings.model_copy(update=overrides)
    app = create_app(settings, api.clock)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="on", log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100000):  # wait for the server to bind
        if server.started:
            break
        await asyncio.sleep(0)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", app
    finally:
        server.should_exit = True
        await task


@asynccontextmanager
async def relay(api: Api) -> AsyncIterator[None]:
    redis = Redis.from_url(api.settings.redis_url)
    task = asyncio.create_task(run_relay(api.app.state.engine, api.sessions, redis))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await redis.aclose()


class Stream:
    """Reads one SSE response event by event."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.raw = ""
        self._lines = response.aiter_lines()

    async def next(self, within: float = 10) -> dict[str, Any]:
        """The next event (heartbeats are returned as {"event": "heartbeat"})."""
        return await asyncio.wait_for(self._next(), within)

    async def _next(self) -> dict[str, Any]:
        event: dict[str, Any] = {}
        async for line in self._lines:
            self.raw += line + "\n"
            if line.startswith(": heartbeat"):
                return {"event": "heartbeat"}
            if line.startswith("retry:"):
                continue
            if not line:
                if event:
                    return event
                continue
            key, _, value = line.partition(": ")
            if key == "data":
                event["data"] = json.loads(value)
            elif key == "id":
                event["id"] = int(value)
            else:
                event[key] = value
        raise EOFError(self.raw[-500:])

    async def expect(self, kind: str, within: float = 10) -> dict[str, Any]:
        """The next event of this kind, skipping heartbeats."""
        while True:
            event = await self.next(within)
            if event["event"] != "heartbeat":
                assert event["event"] == kind, event
                return event


@asynccontextmanager
async def open_stream(client: httpx.AsyncClient, url: str, **headers: str) -> AsyncIterator[Stream]:
    async with client.stream("GET", url, headers=headers) as response:
        yield Stream(response)
