import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from redis.asyncio import Redis

from app.realtime.relay import HEALTH_KEY, relay_healthy
from tests.integration.api import Api

pytestmark = pytest.mark.anyio

COMPOSE = Path(__file__).resolve().parents[3] / "docker-compose.yml"


async def test_the_relay_signal_goes_stale_when_the_relay_task_dies(
    api: Api, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REDIS_URL", redis_url)
    from app.workers import health
    from app.workers import main as worker

    redis = Redis.from_url(redis_url)
    await redis.delete(HEALTH_KEY)
    ctx: dict[str, Any] = {"settings": api.settings}
    await worker.startup(ctx)  # starts the relay task
    try:
        for _ in range(100000):
            if await redis.get(HEALTH_KEY) is not None:
                break
            await asyncio.sleep(0)
        assert await relay_healthy(redis, time.time(), health.RELAY_STALE_SEC)
        ctx["relay"].cancel()  # the relay dies
        await asyncio.gather(ctx["relay"], return_exceptions=True)
        later = time.time() + health.RELAY_STALE_SEC + 1
        assert not await relay_healthy(redis, later, health.RELAY_STALE_SEC)
        await redis.delete(HEALTH_KEY)
        assert not await relay_healthy(redis, time.time(), health.RELAY_STALE_SEC)  # never ran
    finally:
        await worker.shutdown(ctx)  # copes with a relay task that was already cancelled
        await redis.aclose()


def test_compose_worker_healthcheck_uses_the_relay_signal() -> None:
    text = COMPOSE.read_text()
    worker = text[text.index("  worker:") :]
    assert '"python", "-m", "app.workers.health"' in worker.split("healthcheck:")[1].split("\n")[1]
