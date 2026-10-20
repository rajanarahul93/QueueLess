import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND = Path(__file__).resolve().parents[2]


def alembic_config(url: str) -> Config:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "app/db/migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


async def installed_extensions(url: str) -> set[str]:
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        rows = await connection.execute(text("SELECT extname FROM pg_extension"))
    await engine.dispose()
    return {row[0] for row in rows}


def test_baseline_upgrade_downgrade_upgrade(postgres_url: str) -> None:
    config = alembic_config(postgres_url)
    command.upgrade(config, "head")
    assert {"btree_gist", "citext"} <= asyncio.run(installed_extensions(postgres_url))
    command.downgrade(config, "base")
    assert not {"btree_gist", "citext"} & asyncio.run(installed_extensions(postgres_url))
    command.upgrade(config, "head")
