import asyncio

from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.session import create_engine
from app.models import Base
from tests.integration.db import alembic_config, reset_schema

CONSTRAINTS_SQL = """
SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE connamespace = 'public'::regnamespace AND conrelid::regclass::text <> 'alembic_version'
"""
INDEXES_SQL = """
SELECT indexname, indexdef FROM pg_indexes
WHERE schemaname = 'public' AND tablename <> 'alembic_version'
"""


async def query(url: str, sql: str) -> list[tuple[str, ...]]:
    engine = create_engine(url)
    async with engine.connect() as connection:
        rows = [tuple(row) for row in await connection.execute(text(sql))]
    await engine.dispose()
    return rows


async def create_database_from_models(url: str) -> str:
    """A second database built with create_all, to compare against the migrated one."""
    admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text("DROP DATABASE IF EXISTS models_only"))
        await connection.execute(text("CREATE DATABASE models_only"))
    await admin.dispose()
    models_url = make_url(url).set(database="models_only").render_as_string(hide_password=False)
    engine = create_engine(models_url)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION btree_gist"))
        await connection.execute(text("CREATE EXTENSION citext"))
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    return models_url


def test_upgrade_downgrade_upgrade(postgres_url: str) -> None:
    reset_schema(postgres_url)
    config = alembic_config(postgres_url)
    tables_sql = "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    expected = set(Base.metadata.tables)
    assert len(expected) == 13

    command.upgrade(config, "head")
    tables = {row[0] for row in asyncio.run(query(postgres_url, tables_sql))}
    assert tables == expected | {"alembic_version"}
    extensions = {
        row[0] for row in asyncio.run(query(postgres_url, "SELECT extname FROM pg_extension"))
    }
    assert {"btree_gist", "citext"} <= extensions

    command.downgrade(config, "base")
    tables = {row[0] for row in asyncio.run(query(postgres_url, tables_sql))}
    assert tables == {"alembic_version"}
    extensions = {
        row[0] for row in asyncio.run(query(postgres_url, "SELECT extname FROM pg_extension"))
    }
    assert not {"btree_gist", "citext"} & extensions

    command.upgrade(config, "head")


def test_models_and_migrations_agree(migrated_url: str) -> None:
    # Alembic compares tables, columns, types, defaults, FKs, uniques and plain indexes.
    command.check(alembic_config(migrated_url))

    # It cannot see CHECKs, exclusion constraints or partial-index predicates, so compare the
    # catalogs of a migrated database and a create_all one: names and definitions, both ways.
    models_url = asyncio.run(create_database_from_models(migrated_url))
    for sql in (CONSTRAINTS_SQL, INDEXES_SQL):
        assert sorted(asyncio.run(query(migrated_url, sql))) == sorted(
            asyncio.run(query(models_url, sql))
        )


def test_no_double_prefixed_check_names(migrated_url: str) -> None:
    for table, name, _ in asyncio.run(query(migrated_url, CONSTRAINTS_SQL)):
        assert not name.startswith(f"ck_{table}_ck_"), name
