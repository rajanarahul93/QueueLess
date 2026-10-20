from typing import Any

from arq.connections import RedisSettings

from app.core.config import Settings


async def noop(ctx: dict[str, Any]) -> None:
    # arq refuses to start with no registered job. Removed when the first real
    # worker job lands in Phase 3.
    return None


class WorkerSettings:
    functions = [noop]
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)
    health_check_interval = 30
