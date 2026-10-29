import asyncio
from pathlib import Path

from alembic.config import Config
from sqlalchemy import text

from app.db.session import create_engine

BACKEND = Path(__file__).resolve().parents[2]


def alembic_config(url: str) -> Config:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "app/db/migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


async def _reset_schema(url: str) -> None:
    engine = create_engine(url)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


def reset_schema(url: str) -> None:
    """Empty database, extensions included; migration 0001 recreates them."""
    asyncio.run(_reset_schema(url))
