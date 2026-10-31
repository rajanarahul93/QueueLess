"""The one writer of outbox events (FR-32): a row in the same transaction as the change.

Ordering: a queue's events must commit in id order, or a relay or a replaying client could skip
a lower id that commits late. Every queue-scoped event therefore takes the queue's advisory lock
right before its id is drawn, and holds it until commit, so the next event of that queue can
only draw a higher id after this one is committed. Business-scoped events (appointments) have no
queue and no ordering guarantee; no stream replays them.
"""

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OutboxEvent, QueueToken

MAX_PAYLOAD_BYTES = 4096
TOKEN_FIELDS = (
    "token_no",
    "status",
    "priority",
    "version",
    "source",
    "skip_count",
)
TOKEN_TIMES = ("joined_at", "queued_at", "called_at", "started_at", "completed_at")


def queue_lock_key(queue_id: uuid.UUID) -> int:
    """A signed 64-bit key from the queue id's first 8 bytes. Queue ids are random UUIDs, so two
    queues sharing a key is vanishingly rare, and would only make them wait for each other."""
    return int.from_bytes(queue_id.bytes[:8], "big", signed=True)


async def lock_queue_events(session: AsyncSession, queue_id: uuid.UUID) -> None:
    # why raw SQL: SQLAlchemy has no construct for advisory locks. Held until commit; re-taking
    # it in the same transaction (call-next already holds it) does not wait.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": queue_lock_key(queue_id)}
    )


async def emit_event(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    queue_id: uuid.UUID | None,
    event_type: str,
    payload: dict[str, Any],
    at: datetime,
) -> None:
    """Adds an outbox row to the caller's transaction; never commits. Call it after every row
    lock the change needs, just before the commit. Payloads hold ids, numbers, statuses and
    times only: never names, phones, emails or customer ids."""
    if len(json.dumps(payload, default=str).encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"outbox payload over {MAX_PAYLOAD_BYTES} bytes: {event_type}")
    if queue_id is not None:
        await lock_queue_events(session, queue_id)
    await session.execute(
        insert(OutboxEvent).values(
            business_id=business_id,
            queue_id=queue_id,
            event_type=event_type,
            payload=payload,
            created_at=at,
        )
    )


async def emit_token_event(
    session: AsyncSession, token_id: uuid.UUID, event_type: str, at: datetime, **extra: object
) -> None:
    """A queue event for one token, built from the token as this transaction now sees it."""
    token = (
        await session.execute(select(QueueToken).where(QueueToken.id == token_id))
    ).scalar_one()
    await session.refresh(token)
    payload: dict[str, Any] = {"token_id": str(token.id)}
    payload.update({field: getattr(token, field) for field in TOKEN_FIELDS})
    for field in TOKEN_TIMES:
        value = getattr(token, field)
        payload[field] = value.isoformat() if value is not None else None
    payload.update(extra)
    await emit_event(
        session,
        business_id=token.business_id,
        queue_id=token.queue_id,
        event_type=event_type,
        payload=payload,
        at=at,
    )
