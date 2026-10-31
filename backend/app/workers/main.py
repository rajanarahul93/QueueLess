import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from app.core.config import Settings
from app.db.session import create_engine, create_session_factory
from app.services import queue_jobs

JOB_TIMEOUT_SEC = 50  # shorter than the one-minute schedule
WORKER_POOL_SIZE = 5


# ctx is arq's per-worker context dict: everything is created at startup and closed at shutdown.
async def startup(ctx: dict[str, Any]) -> None:
    app_logger = logging.getLogger("app")
    if not app_logger.handlers:  # arq configures only its own logger; job summaries are INFO
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        app_logger.addHandler(handler)
        app_logger.setLevel(logging.INFO)
    settings = ctx.get("settings") or Settings()
    engine = create_engine(settings.database_url, WORKER_POOL_SIZE)
    ctx["engine"] = engine
    ctx["sessions"] = create_session_factory(engine)
    ctx.setdefault("clock", time.time)


async def shutdown(ctx: dict[str, Any]) -> None:
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


def _every_minute(job: Any) -> Any:  # Any: arq's cron job type is internal
    # unique: one run per scheduled minute across workers; no retries, the next minute picks up.
    return cron(job, second=0, timeout=JOB_TIMEOUT_SEC, max_tries=1, unique=True)


class WorkerSettings:
    cron_jobs = [
        _every_minute(sweep_no_shows),
        _every_minute(promote_waiting),
        _every_minute(end_of_day),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(os.environ["REDIS_URL"])
    health_check_interval = 30
