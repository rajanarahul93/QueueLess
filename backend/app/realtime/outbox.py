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

from sqlalchemy import Select, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Appointment, OutboxEvent, QueueToken, Service, ServiceSession

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

# A service session is written only when a token becomes in_service, and transitions.py reaches
# in_service only from called via "start" and leaves it only via "complete". So a token with no
# appointment can hold a service only once its status is one of these; before that there is
# nothing to join to. The walk-in join, the one event emitted under burst inside the queue row
# lock, therefore keeps the plain single-row read.
SERVICE_STATUSES = frozenset({"in_service", "completed"})

# A token carries no service column: an appointment token's service is its appointment's, a
# walk-in's is the session staff opened at Start, and a walk-in before Start has none. One
# definition here, used by the event payloads and by the staff read paths.
SERVICE_OF_TOKEN = func.coalesce(Appointment.service_id, ServiceSession.service_id)


def with_service(query: Select[*tuple[Any, ...]]) -> Select[*tuple[Any, ...]]:
    """Adds service_id and service_name to a query that selects QueueToken, in the same
    statement: the joins are outer, so an unknown service is simply null. A token reaches
    in_service at most once, so there is never more than one session row to join."""
    return (
        query.add_columns(SERVICE_OF_TOKEN.label("service_id"), Service.name.label("service_name"))
        .outerjoin(Appointment, Appointment.id == QueueToken.appointment_id)
        .outerjoin(ServiceSession, ServiceSession.token_id == QueueToken.id)
        .outerjoin(Service, Service.id == SERVICE_OF_TOKEN)
    )


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
    # populate_existing: this transaction has usually just UPDATEd the row, so the identity
    # map's copy is stale; reading it fresh here does what a second refresh used to do.
    token = (
        await session.execute(
            select(QueueToken)
            .where(QueueToken.id == token_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if token.appointment_id is None and token.status not in SERVICE_STATUSES:
        service_id, service_name = None, None
    else:
        _, service_id, service_name = (
            await session.execute(
                with_service(select(QueueToken.id).where(QueueToken.id == token_id))
            )
        ).one()
    payload: dict[str, Any] = {"token_id": str(token.id)}
    payload.update({field: getattr(token, field) for field in TOKEN_FIELDS})
    for field in TOKEN_TIMES:
        value = getattr(token, field)
        payload[field] = value.isoformat() if value is not None else None
    # The name as the service is called now; the staff snapshot resolves it again on every read,
    # so a rename after this event shows at the next snapshot.
    payload["service_id"] = str(service_id) if service_id is not None else None
    payload["service_name"] = service_name
    payload.update(extra)
    await emit_event(
        session,
        business_id=token.business_id,
        queue_id=token.queue_id,
        event_type=event_type,
        payload=payload,
        at=at,
    )
