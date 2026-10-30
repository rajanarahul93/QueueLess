from collections.abc import AsyncIterator

import pytest
from alembic import command

from tests.integration.api import Api, open_api
from tests.integration.db import alembic_config, reset_schema


@pytest.fixture(scope="module")
def migrated_url(postgres_url: str) -> str:
    reset_schema(postgres_url)
    command.upgrade(alembic_config(postgres_url), "head")
    return postgres_url


@pytest.fixture
async def api(migrated_url: str, redis_url: str) -> AsyncIterator[Api]:
    """A pool big enough for 50 simultaneous requests and limits that do not interfere."""
    overrides: dict[str, object] = {"db_pool_size": 50, "booking_rate_limit_per_hour": 1000}
    async with open_api(migrated_url, redis_url, overrides) as opened:
        yield opened
