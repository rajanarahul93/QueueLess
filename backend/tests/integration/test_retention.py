import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import func, insert, select

from app.models import AuditLog, OutboxEvent, RefreshToken
from app.services.retention import clean, twelve_months_before
from tests.integration.api import Api
from tests.integration.db import alembic_config, reset_schema

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


@pytest.fixture
def migrated_url(postgres_url: str) -> str:
    reset_schema(postgres_url)
    command.upgrade(alembic_config(postgres_url), "head")
    return postgres_url


async def count(api: Api, model: type) -> int:
    async with api.sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_boundaries_of_each_table(api: Api) -> None:
    user = await api.create_user("owner", email="keep@example.test")
    week = timedelta(days=7)
    second = timedelta(seconds=1)
    async with api.sessions() as session:
        await session.execute(
            insert(OutboxEvent),
            [
                {
                    "business_id": uuid.uuid4(),
                    "event_type": "old",
                    "payload": {},
                    "created_at": NOW,
                    "published_at": NOW - week - second,
                },
                {
                    "business_id": uuid.uuid4(),
                    "event_type": "edge",
                    "payload": {},
                    "created_at": NOW,
                    "published_at": NOW - week,
                },
                {
                    "business_id": uuid.uuid4(),
                    "event_type": "unpublished",
                    "payload": {},
                    "created_at": NOW - week * 10,
                    "published_at": None,
                },
            ],
        )
        year_ago = twelve_months_before(NOW)
        await session.execute(
            insert(AuditLog),
            [
                {
                    "business_id": uuid.uuid4(),
                    "actor_id": None,
                    "action": "old",
                    "entity_type": "x",
                    "created_at": year_ago - second,
                },
                {
                    "business_id": uuid.uuid4(),
                    "actor_id": None,
                    "action": "edge",
                    "entity_type": "x",
                    "created_at": year_ago,
                },
            ],
        )
        await session.execute(
            insert(RefreshToken),
            [
                {
                    "user_id": user.id,
                    "family_id": uuid.uuid4(),
                    "token_hash": "a",
                    "expires_at": NOW - week - second,
                },
                {
                    "user_id": user.id,
                    "family_id": uuid.uuid4(),
                    "token_hash": "b",
                    "expires_at": NOW - week,
                },
                # revoked but not expired: kept for reuse detection
                {
                    "user_id": user.id,
                    "family_id": uuid.uuid4(),
                    "token_hash": "c",
                    "expires_at": NOW + week,
                    "revoked_at": NOW - week * 4,
                },
            ],
        )
        await session.commit()
    summary = await clean(api.sessions, NOW)
    assert summary.changed == 3
    async with api.sessions() as session:
        outbox = set((await session.scalars(select(OutboxEvent.event_type))).all())
        audit = set((await session.scalars(select(AuditLog.action))).all())
        refresh = set((await session.scalars(select(RefreshToken.token_hash))).all())
    assert outbox == {"edge", "unpublished"} and audit == {"edge"} and refresh == {"b", "c"}
    again = await clean(api.sessions, NOW)
    assert again.changed == 0  # idempotent


async def test_bounded_batches_drain_over_runs(api: Api) -> None:
    old = NOW - timedelta(days=30)
    async with api.sessions() as session:
        await session.execute(
            insert(OutboxEvent),
            [
                {
                    "business_id": uuid.uuid4(),
                    "event_type": "x",
                    "payload": {},
                    "created_at": old,
                    "published_at": old,
                }
                for _ in range(5)
            ],
        )
        await session.commit()
    runs = [(await clean(api.sessions, NOW, batch=2)).changed for _ in range(4)]
    assert runs == [2, 2, 1, 0]
    assert await count(api, OutboxEvent) == 0


def test_twelve_months_handles_a_leap_day() -> None:
    assert twelve_months_before(datetime(2028, 2, 29, tzinfo=UTC)) == datetime(
        2027, 2, 28, tzinfo=UTC
    )
