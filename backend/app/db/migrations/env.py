import asyncio

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings

config = context.config
# Tests inject a container URL on the config; otherwise use the environment.
url = config.get_main_option("sqlalchemy.url") or Settings().database_url
# No models yet; Phase 1 sets target_metadata for autogenerate.
target_metadata = None


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


asyncio.run(run_async_migrations())
