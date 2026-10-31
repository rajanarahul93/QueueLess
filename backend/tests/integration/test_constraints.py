import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Executable, Insert, insert, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.models import (
    Appointment,
    Business,
    DailyStat,
    Queue,
    QueueToken,
    Resource,
    Service,
    User,
)

pytestmark = pytest.mark.anyio

NINE_AM = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


@dataclass
class Seed:
    business_id: uuid.UUID
    resource_id: uuid.UUID
    service_id: uuid.UUID
    queue_id: uuid.UUID
    customer_id: uuid.UUID


async def rejects(
    conn: AsyncConnection,
    statement: Executable,
    match: str,
    error: type[DBAPIError] = IntegrityError,
) -> None:
    """Savepoint so the test can keep using the transaction after the expected failure."""
    with pytest.raises(error, match=match):
        async with conn.begin_nested():
            await conn.execute(statement)


async def new_user(conn: AsyncConnection, **overrides: Any) -> uuid.UUID:
    values: dict[str, Any] = {
        "name": "u",
        "phone": f"+91{uuid.uuid4().int % 10**10:010d}",
        "role": "customer",
    } | overrides
    return (await conn.execute(insert(User).values(**values).returning(User.id))).scalar_one()


@pytest.fixture
async def seed(conn: AsyncConnection) -> Seed:
    owner = await new_user(conn, role="owner")
    business_id = (
        await conn.execute(
            insert(Business)
            .values(
                owner_id=owner,
                name="b",
                category="clinic",
                timezone="Asia/Kolkata",
                open_hours={},
                settings={},
            )
            .returning(Business.id)
        )
    ).scalar_one()
    resource_id = (
        await conn.execute(
            insert(Resource).values(business_id=business_id, name="r").returning(Resource.id)
        )
    ).scalar_one()
    service_id = (
        await conn.execute(
            insert(Service)
            .values(business_id=business_id, name="s", duration_min=30, price=Decimal("10"))
            .returning(Service.id)
        )
    ).scalar_one()
    queue_id = (
        await conn.execute(
            insert(Queue)
            .values(
                business_id=business_id,
                resource_id=resource_id,
                queue_date=NINE_AM.date(),
                status="open",
            )
            .returning(Queue.id)
        )
    ).scalar_one()
    return Seed(business_id, resource_id, service_id, queue_id, await new_user(conn))


def book(
    seed: Seed,
    during: Range[datetime],
    status: str = "booked",
    resource_id: uuid.UUID | None = None,
    **extra: Any,
) -> Insert:
    return insert(Appointment).values(
        business_id=seed.business_id,
        service_id=seed.service_id,
        resource_id=resource_id or seed.resource_id,
        customer_id=seed.customer_id,
        during=during,
        status=status,
        **extra,
    )


def slot(start_min: int, end_min: int) -> Range[datetime]:
    return Range(
        NINE_AM + timedelta(minutes=start_min), NINE_AM + timedelta(minutes=end_min), bounds="[)"
    )


def token(seed: Seed, customer_id: uuid.UUID, token_no: int, **overrides: Any) -> Insert:
    values: dict[str, Any] = {
        "business_id": seed.business_id,
        "queue_id": seed.queue_id,
        "token_no": token_no,
        "source": "walk_in",
        "customer_id": customer_id,
        "status": "waiting",
    } | overrides
    return insert(QueueToken).values(**values)


# --- users -----------------------------------------------------------------------------------


async def test_user_needs_email_or_phone(conn: AsyncConnection) -> None:
    await rejects(
        conn,
        insert(User).values(name="u", role="customer"),
        "ck_users_email_or_phone",
    )
    await new_user(conn, phone=None, email="a@example.com")
    await new_user(conn, phone="+911111111111", email=None)


async def test_user_email_and_phone_are_unique(conn: AsyncConnection) -> None:
    await new_user(conn, email="a@example.com", phone="+911111111111")
    await rejects(
        conn,
        insert(User).values(name="u", role="customer", email="A@Example.com"),
        "uq_users_email",
    )
    await rejects(
        conn,
        insert(User).values(name="u", role="customer", phone="+911111111111"),
        "uq_users_phone",
    )
    # NULLs never collide, so many OTP-only and many email-only users can coexist.
    await new_user(conn, phone=None, email="b@example.com")
    await new_user(conn, phone=None, email="c@example.com")


async def test_user_role_check(conn: AsyncConnection) -> None:
    await rejects(conn, insert(User).values(name="u", phone="+912", role="boss"), "ck_users_role")


# --- appointments ----------------------------------------------------------------------------


async def test_no_overlap_rejects_overlapping_active_appointments(
    conn: AsyncConnection, seed: Seed
) -> None:
    await conn.execute(book(seed, slot(0, 30)))
    await rejects(conn, book(seed, slot(15, 45)), "no_overlap")


async def test_no_overlap_one_minute_overlap_rejected_back_to_back_allowed(
    conn: AsyncConnection, seed: Seed
) -> None:
    await conn.execute(book(seed, slot(0, 30)))
    await rejects(conn, book(seed, slot(29, 59)), "no_overlap")
    await conn.execute(book(seed, slot(30, 60)))  # 10:00-10:30 then 10:30-11:00


async def test_no_overlap_ignores_cancelled_and_other_resources(
    conn: AsyncConnection, seed: Seed
) -> None:
    await conn.execute(book(seed, slot(0, 30), status="cancelled"))
    await conn.execute(book(seed, slot(0, 30)))
    other_resource = (
        await conn.execute(
            insert(Resource).values(business_id=seed.business_id, name="r2").returning(Resource.id)
        )
    ).scalar_one()
    await conn.execute(book(seed, slot(0, 30), resource_id=other_resource))


@pytest.mark.parametrize(
    "during",
    [
        pytest.param(Range(empty=True), id="empty"),
        pytest.param(Range(NINE_AM, NINE_AM + timedelta(minutes=30), bounds="[]"), id="closed"),
        pytest.param(Range(NINE_AM, NINE_AM + timedelta(minutes=30), bounds="(]"), id="open-start"),
        pytest.param(Range(NINE_AM, None, bounds="[)"), id="unbounded-end"),
        pytest.param(Range(None, NINE_AM, bounds="[)"), id="unbounded-start"),
        pytest.param(Range(NINE_AM, NINE_AM, bounds="[)"), id="start-equals-end"),
    ],
)
async def test_range_valid_rejects_degenerate_ranges(
    conn: AsyncConnection, seed: Seed, during: Range[datetime]
) -> None:
    await rejects(conn, book(seed, during), "ck_appointments_range_valid")


async def test_range_with_start_after_end_is_rejected(conn: AsyncConnection, seed: Seed) -> None:
    # Postgres refuses to build the range at all (DataError, not IntegrityError).
    await rejects(
        conn,
        book(seed, slot(30, 0)),
        "range lower bound must be less than or equal to range upper bound",
        DBAPIError,
    )


async def test_appointment_status_and_priority_checks(conn: AsyncConnection, seed: Seed) -> None:
    await rejects(conn, book(seed, slot(0, 30), status="done"), "ck_appointments_status")
    await rejects(conn, book(seed, slot(0, 30), priority=3), "ck_appointments_priority_range")
    await rejects(conn, book(seed, slot(0, 30), priority=-1), "ck_appointments_priority_range")
    await conn.execute(book(seed, slot(0, 30), priority=0))
    await conn.execute(book(seed, slot(30, 60), priority=2))


# --- queues and tokens -----------------------------------------------------------------------


async def test_one_queue_per_resource_per_day(conn: AsyncConnection, seed: Seed) -> None:
    duplicate = insert(Queue).values(
        business_id=seed.business_id,
        resource_id=seed.resource_id,
        queue_date=NINE_AM.date(),
        status="open",
    )
    await rejects(conn, duplicate, "uq_queues_resource_date")
    await rejects(
        conn,
        insert(Queue).values(
            business_id=seed.business_id,
            resource_id=seed.resource_id,
            queue_date=NINE_AM.date() + timedelta(days=1),
            status="reopened",
        ),
        "ck_queues_status",
    )


async def test_uq_token_rejects_duplicate_token_no(conn: AsyncConnection, seed: Seed) -> None:
    await conn.execute(token(seed, seed.customer_id, 1))
    await rejects(conn, token(seed, await new_user(conn), 1), "uq_token")


async def test_one_active_token_per_customer_per_queue(conn: AsyncConnection, seed: Seed) -> None:
    first = (
        await conn.execute(token(seed, seed.customer_id, 1).returning(QueueToken.id))
    ).scalar_one()
    await rejects(conn, token(seed, seed.customer_id, 2), "one_active_token")

    await conn.execute(update(QueueToken).where(QueueToken.id == first).values(status="completed"))
    await conn.execute(token(seed, seed.customer_id, 2))


async def test_one_token_per_appointment(conn: AsyncConnection, seed: Seed) -> None:
    appointment_id = (
        await conn.execute(book(seed, slot(0, 30)).returning(Appointment.id))
    ).scalar_one()
    await conn.execute(
        token(seed, seed.customer_id, 1, source="appointment", appointment_id=appointment_id)
    )
    await rejects(
        conn,
        token(
            seed,
            await new_user(conn),
            2,
            source="appointment",
            appointment_id=appointment_id,
        ),
        "uq_queue_tokens_appointment_id",
    )
    # NULL appointment_id (walk-ins) is unlimited.
    await conn.execute(token(seed, await new_user(conn), 3))
    await conn.execute(token(seed, await new_user(conn), 4))


async def test_token_checks_and_not_null(conn: AsyncConnection, seed: Seed) -> None:
    await rejects(
        conn, token(seed, seed.customer_id, 1, status="skipped"), "ck_queue_tokens_status"
    )
    await rejects(conn, token(seed, seed.customer_id, 1, source="phone"), "ck_queue_tokens_source")
    await rejects(
        conn, token(seed, seed.customer_id, 1, priority=3), "ck_queue_tokens_priority_range"
    )
    await rejects(conn, token(seed, None, 1), 'null value in column "customer_id"')  # type: ignore[arg-type]
    await rejects(
        conn, token(seed, seed.customer_id, 1, queued_at=None), 'null value in column "queued_at"'
    )
    await conn.execute(token(seed, seed.customer_id, 1, priority=2))


# --- services and daily_stats ----------------------------------------------------------------


async def test_service_duration_and_buffer_checks(conn: AsyncConnection, seed: Seed) -> None:
    def service(duration: int, buffer: int) -> Executable:
        return insert(Service).values(
            business_id=seed.business_id,
            name="s",
            duration_min=duration,
            buffer_min=buffer,
            price=Decimal("0"),
        )

    await rejects(conn, service(0, 0), "ck_services_duration_min_positive")
    await rejects(conn, service(30, -1), "ck_services_buffer_min_non_negative")
    await conn.execute(service(1, 0))


async def test_daily_stats_hour_range(conn: AsyncConnection, seed: Seed) -> None:
    def stat(hour: int | None) -> Executable:
        return insert(DailyStat).values(
            business_id=seed.business_id,
            service_id=seed.service_id,
            stat_date=NINE_AM.date(),
            hour=hour,
        )

    await rejects(conn, stat(-1), "ck_daily_stats_hour_range")
    await rejects(conn, stat(24), "ck_daily_stats_hour_range")
    await conn.execute(stat(0))
    await conn.execute(stat(23))
    await conn.execute(stat(None))  # NULL: the whole-day row; the CHECK lets it through


async def test_daily_stats_key_treats_nulls_as_equal(conn: AsyncConnection, seed: Seed) -> None:
    """NULLS NOT DISTINCT: a second whole-day row (hour NULL) or a second never-started
    walk-in row (service NULL) of the same day is a duplicate; any other key value is not."""
    day = NINE_AM.date()

    def stat(service_id: uuid.UUID | None, hour: int | None, **other: Any) -> Executable:
        return insert(DailyStat).values(
            business_id=other.get("business_id", seed.business_id),
            service_id=service_id,
            stat_date=other.get("stat_date", day),
            hour=hour,
        )

    for service_id, hour in ((seed.service_id, None), (None, 9), (None, None)):
        await conn.execute(stat(service_id, hour))
        await rejects(conn, stat(service_id, hour), "uq_daily_stats_key")
    await conn.execute(stat(seed.service_id, 9))  # a different hour from the whole-day row
    await conn.execute(stat(None, 10))  # a different hour of the NULL service
    await conn.execute(stat(None, None, stat_date=day + timedelta(days=1)))  # another day
    other_business = await conn.scalar(
        insert(Business)
        .values(
            owner_id=await new_user(conn, role="owner"),
            name="Other",
            category="c",
            timezone="Asia/Kolkata",
            open_hours={},
            settings={},
        )
        .returning(Business.id)
    )
    await conn.execute(stat(None, None, business_id=other_business))  # another business
