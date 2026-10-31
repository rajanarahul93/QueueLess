"""Outbox relay (FR-32): committed outbox rows to Redis Pub/Sub, then marked published.

No high-water mark: every pass reads `published_at IS NULL ORDER BY id`. A queue's events commit
in id order (see emit_event), so the visible unpublished rows of a queue are always a prefix of
its remaining ids; a lower id of ANOTHER queue that commits late is simply picked up by a later
pass. Only one relay runs at a time (a session advisory lock), so a queue is published in order.
Delivery is at least once: a crash between PUBLISH and the mark republishes those rows, and the
stream layer drops ids it has already sent.
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models import OutboxEvent

logger = logging.getLogger(__name__)

BATCH = 500
IDLE_SEC = 0.25
MAX_BACKOFF_SEC = 10.0
HEALTH_KEY = "relay:last_pass"
RELAY_LOCK_KEY = 0x51_75_65_75_65_52_6C_79  # a fixed key for the one-relay session lock


def channel(business_id: uuid.UUID, queue_id: uuid.UUID | None) -> str:
    return f"queue:{queue_id}" if queue_id is not None else f"business:{business_id}"


def envelope(row: OutboxEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "type": row.event_type,
        "queue_id": str(row.queue_id) if row.queue_id else None,
        "occurred_at": row.created_at.isoformat(),
        "data": row.payload,
    }


async def relay_pass(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, batch: int = BATCH
) -> int:
    """Publishes up to `batch` unpublished rows in id order, then marks exactly those. A Redis
    error stops before the mark, so the rows stay unpublished and are retried."""
    async with sessions() as session:
        rows = list(
            (
                await session.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.published_at.is_(None))
                    .order_by(OutboxEvent.id)
                    .limit(batch)
                )
            ).all()
        )
        if not rows:
            return 0
        for row in rows:
            await redis.publish(channel(row.business_id, row.queue_id), json.dumps(envelope(row)))
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_([row.id for row in rows]))
            .values(published_at=datetime.now(UTC))
        )
        await session.commit()
        return len(rows)


async def run_relay(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession], redis: Redis
) -> None:
    """The worker's relay loop. Holds the one-relay lock on a connection of its own; records
    each completed pass in Redis for the health check. Never returns; cancel it to stop."""
    backoff = 1.0
    while True:
        try:
            async with engine.connect() as holder:
                # why raw SQL: a session-level advisory lock; it lives as long as this connection.
                if not await holder.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": RELAY_LOCK_KEY}
                ):
                    await asyncio.sleep(MAX_BACKOFF_SEC)  # another worker relays
                    continue
                await holder.commit()  # end the transaction; the session lock stays
                while True:
                    published = await relay_pass(sessions, redis)
                    await redis.set(HEALTH_KEY, str(time.time()))
                    backoff = 1.0
                    if published < BATCH:
                        await asyncio.sleep(IDLE_SEC)
        except (RedisError, SQLAlchemyError, OSError) as error:
            logger.warning("relay pass failed error=%s; retrying", type(error).__name__)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SEC)


async def relay_healthy(redis: Redis, now: float, stale_after_sec: float) -> bool:
    """True when a relay pass completed within the last `stale_after_sec` seconds."""
    raw = await redis.get(HEALTH_KEY)
    return raw is not None and now - float(raw) <= stale_after_sec
