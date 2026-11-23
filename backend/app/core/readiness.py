"""Readiness: can this API serve traffic. Three cheap checks, run side by side, each time-boxed.

The worker has no HTTP port, so its health is the relay signal it already writes to Redis after
every outbox pass (`relay_healthy`, the same signal the Compose healthcheck uses).
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.realtime.relay import relay_healthy
from app.workers.health import RELAY_STALE_SEC

CHECK_TIMEOUT_SEC = 2.0
# Everything a check can raise when its dependency is down, slow, or answers nonsense.
DEPENDENCY_ERRORS = (TimeoutError, OSError, RedisError, SQLAlchemyError, ValueError)

Verdict = Literal["ok", "failed"]


class ReadyOut(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[str, Verdict]


async def _verdict(check: Callable[[], Awaitable[object]]) -> Verdict:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SEC):
            passed = await check()
    except DEPENDENCY_ERRORS:
        return "failed"
    return "ok" if passed else "failed"


async def check_ready(engine: AsyncEngine, redis: Redis, now: float) -> ReadyOut:
    async def postgres() -> bool:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    async def redis_ping() -> bool:
        return bool(await redis.ping())

    async def relay() -> bool:
        return await relay_healthy(redis, now, RELAY_STALE_SEC)

    names = ("postgres", "redis", "relay")
    results = await asyncio.gather(*(_verdict(check) for check in (postgres, redis_ping, relay)))
    checks = dict(zip(names, results, strict=True))
    return ReadyOut(
        status="ok" if all(result == "ok" for result in results) else "unavailable", checks=checks
    )
