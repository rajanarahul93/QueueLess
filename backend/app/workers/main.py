import os
from typing import Any

from arq.connections import RedisSettings


async def noop(ctx: dict[str, Any]) -> None:
    # arq refuses to start with no registered job. Removed when the first real
    # worker job lands in Phase 3.
    return None


# Reads only REDIS_URL: the worker does not need the API's secrets.
class WorkerSettings:
    functions = [noop]
    redis_settings = RedisSettings.from_dsn(os.environ["REDIS_URL"])
    health_check_interval = 30
