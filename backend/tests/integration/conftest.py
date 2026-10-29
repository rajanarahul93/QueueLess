from collections.abc import AsyncIterator

import pytest
from alembic import command
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.session import create_engine
from tests.integration.api import Api, open_api
from tests.integration.db import alembic_config, reset_schema


@pytest.fixture(scope="module")
def migrated_url(postgres_url: str) -> str:
    """Fresh schema at head, once per test module."""
    reset_schema(postgres_url)
    command.upgrade(alembic_config(postgres_url), "head")
    return postgres_url


@pytest.fixture
async def conn(migrated_url: str) -> AsyncIterator[AsyncConnection]:
    """A connection whose transaction is rolled back, so tests leave no rows behind."""
    engine = create_engine(migrated_url)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        yield connection
        await transaction.rollback()
    await engine.dispose()


@pytest.fixture
async def api(migrated_url: str, redis_url: str) -> AsyncIterator[Api]:
    async with open_api(migrated_url, redis_url) as opened:
        yield opened
