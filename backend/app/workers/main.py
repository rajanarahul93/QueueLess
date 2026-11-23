import asyncio
import contextlib
import os
import time
from datetime import UTC, datetime
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from redis.asyncio import Redis

from app.core.config import SCRUBBED_ENVS, Settings, configure_logging
from app.core.log_scrub import install_log_scrub
from app.db.session import create_engine, create_session_factory
from app.realtime.relay import run_relay
from app.services import queue_jobs, retention, rollups

JOB_TIMEOUT_SEC = 50  # shorter than the one-minute schedule
WORKER_POOL_SIZE = 5


# ctx is arq's per-worker context dict: everything is created at startup and closed at shutdown.
async def startup(ctx: dict[str, Any]) -> None:
    settings = ctx.get("settings") or Settings()
    configure_logging(settings.log_level)  # arq configures only its own logger
    if settings.app_env in SCRUBBED_ENVS:
        install_log_scrub()
    engine = create_engine(settings.database_url, WORKER_POOL_SIZE)
    ctx["engine"] = engine
    ctx["sessions"] = create_session_factory(engine)
    ctx.setdefault("clock", time.time)
    ctx["relay_redis"] = Redis.from_url(settings.redis_url)
    # The outbox relay runs for the worker's whole life: arq cron cannot run more often than
    # once a minute, too slow for live updates.
    ctx["relay"] = asyncio.create_task(run_relay(engine, ctx["sessions"], ctx["relay_redis"]))


async def shutdown(ctx: dict[str, Any]) -> None:
    ctx["relay"].cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await ctx["relay"]
    await ctx["relay_redis"].aclose()
    await ctx["engine"].dispose()


def _now(ctx: dict[str, Any]) -> datetime:
    return datetime.fromtimestamp(ctx["clock"](), UTC)


async def sweep_no_shows(ctx: dict[str, Any]) -> dict[str, int]:
    summary = await queue_jobs.sweep_no_shows(ctx["sessions"], _now(ctx))
    return vars(summary)


async def promote_waiting(ctx: dict[str, Any]) -> dict[str, int]:
    summary = await queue_jobs.promote_waiting(ctx["sessions"], _now(ctx))
    return vars(summary)


async def end_of_day(ctx: dict[str, Any]) -> dict[str, int]:
    summary = await queue_jobs.end_of_day(ctx["sessions"], _now(ctx))
    return vars(summary)


async def check_running_late(ctx: dict[str, Any]) -> dict[str, int]:
    summary = await queue_jobs.running_late(ctx["sessions"], _now(ctx))
    return vars(summary)


async def rollup_daily(ctx: dict[str, Any]) -> dict[str, int]:
    summary = await rollups.rollup(ctx["sessions"], _now(ctx))
    return vars(summary)


async def clean_retention(ctx: dict[str, Any]) -> dict[str, int]:
    summary = await retention.clean(ctx["sessions"], _now(ctx))
    return vars(summary)


def _every_minute(job: Any) -> Any:  # Any: arq's cron job type is internal
    # unique: one run per scheduled minute across workers; no retries, the next minute picks up.
    return cron(job, second=0, timeout=JOB_TIMEOUT_SEC, max_tries=1, unique=True)


class WorkerSettings:
    cron_jobs = [
        _every_minute(sweep_no_shows),
        _every_minute(promote_waiting),
        _every_minute(end_of_day),
        _every_minute(check_running_late),
        cron(
            rollup_daily,
            minute={5},
            second=0,
            timeout=JOB_TIMEOUT_SEC,
            max_tries=1,
            unique=True,
        ),
        cron(
            clean_retention,
            minute=set(range(0, 60, 10)),
            second=0,
            timeout=JOB_TIMEOUT_SEC,
            max_tries=1,
            unique=True,
        ),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(os.environ["REDIS_URL"])
    health_check_interval = 30
