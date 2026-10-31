"""Worker health for the Compose healthcheck: arq's own check AND a fresh relay pass.

`python -m app.workers.health` exits 0 when healthy, 1 otherwise. The relay records each
completed pass in Redis; if its task died or stalled, no pass is recorded and after
RELAY_STALE_SEC the worker reports unhealthy.
"""

import asyncio
import os
import sys
import time

from arq.connections import RedisSettings
from arq.worker import async_check_health
from redis.asyncio import Redis

from app.realtime.relay import relay_healthy

RELAY_STALE_SEC = 30  # idle passes run every 0.25 s, so 30 s is a wide margin


async def check(redis_url: str, now: float) -> bool:
    arq_ok = await async_check_health(RedisSettings.from_dsn(redis_url)) == 0
    redis = Redis.from_url(redis_url)
    try:
        return arq_ok and await relay_healthy(redis, now, RELAY_STALE_SEC)
    finally:
        await redis.aclose()


def main() -> int:
    healthy = asyncio.run(check(os.environ["REDIS_URL"], time.time()))
    print("worker healthy" if healthy else "worker unhealthy: arq check or relay pass is stale")
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
