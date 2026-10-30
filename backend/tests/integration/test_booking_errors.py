import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import Insert, insert, text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import create_engine
from app.models import Appointment
from app.services.booking import classify_pg_error

pytestmark = pytest.mark.anyio


def hours(start: int, end: int) -> Range[datetime]:
    day = datetime(2026, 11, 2, tzinfo=UTC)
    return Range(day.replace(hour=start), day.replace(hour=end), bounds="[)")


async def test_a_real_deadlock_is_classified_as_deadlock(migrated_url: str) -> None:
    engine: AsyncEngine = create_engine(migrated_url)
    async with engine.begin() as setup:
        await setup.execute(text("CREATE TABLE deadlock_probe (id int PRIMARY KEY)"))
        await setup.execute(text("INSERT INTO deadlock_probe VALUES (1), (2)"))
    try:
        first, second = await engine.connect(), await engine.connect()
        try:
            await first.execute(text("UPDATE deadlock_probe SET id = id WHERE id = 1"))
            await second.execute(text("UPDATE deadlock_probe SET id = id WHERE id = 2"))
            outcomes = await asyncio.gather(
                first.execute(text("UPDATE deadlock_probe SET id = id WHERE id = 2")),
                second.execute(text("UPDATE deadlock_probe SET id = id WHERE id = 1")),
                return_exceptions=True,
            )
        finally:
            await first.rollback()
            await second.rollback()
            await first.close()
            await second.close()
        errors = [o for o in outcomes if isinstance(o, DBAPIError)]
        assert len(errors) == 1  # Postgres aborts exactly one side
        assert classify_pg_error(errors[0]) == "deadlock"
    finally:
        async with engine.begin() as cleanup:
            await cleanup.execute(text("DROP TABLE deadlock_probe"))
        await engine.dispose()


async def test_overlap_is_classified_by_constraint_name_and_others_are_not(
    migrated_url: str,
) -> None:
    engine = create_engine(migrated_url)
    try:
        async with engine.begin() as conn:
            ids = {
                name: uuid.uuid4() for name in ("owner", "customer", "business", "service", "res")
            }
            await conn.execute(
                text(
                    "INSERT INTO users (id, name, role, email) VALUES "
                    "(:o, 'o', 'owner', 'o@x.test'), (:c, 'c', 'customer', 'c@x.test')"
                ),
                {"o": ids["owner"], "c": ids["customer"]},
            )
            await conn.execute(
                text(
                    "INSERT INTO businesses (id, owner_id, name, category, timezone, open_hours, "
                    "settings) VALUES (:b, :o, 'b', 'c', 'UTC', '{}', '{}')"
                ),
                {"b": ids["business"], "o": ids["owner"]},
            )
            await conn.execute(
                text(
                    "INSERT INTO services (id, business_id, name, duration_min, price) "
                    "VALUES (:s, :b, 's', 30, 1)"
                ),
                {"s": ids["service"], "b": ids["business"]},
            )
            await conn.execute(
                text("INSERT INTO resources (id, business_id, name) VALUES (:r, :b, 'r')"),
                {"r": ids["res"], "b": ids["business"]},
            )
        values = {
            "business_id": ids["business"],
            "service_id": ids["service"],
            "resource_id": ids["res"],
            "customer_id": ids["customer"],
        }

        def row(**overrides: object) -> Insert:
            return insert(Appointment).values(
                values | {"during": hours(4, 5), "status": "booked"} | overrides
            )

        async with engine.begin() as conn:
            await conn.execute(row())
        cases = (
            (row(), "overlap"),
            (row(during=hours(8, 9), status="nonsense"), None),  # a CHECK violation
            (row(during=hours(10, 11), customer_id=uuid.uuid4()), None),  # a foreign key
        )
        for statement, expected in cases:
            with pytest.raises(DBAPIError) as error:
                async with engine.begin() as conn:
                    await conn.execute(statement)
            assert classify_pg_error(error.value) == expected
    finally:
        await engine.dispose()
