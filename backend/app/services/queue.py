import uuid
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import ScalarSelect, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.core.timezones import get_zone
from app.models import Business, BusinessMember, Queue, QueueToken, Resource, User
from app.schemas.queue import (
    QueueOut,
    SnapshotCounts,
    SnapshotCustomer,
    SnapshotOut,
    SnapshotToken,
)
from app.services.business import get_business

ACTIVE_TOKEN_STATUSES = ("waiting", "called", "in_service")


async def default_resource(session: AsyncSession, business_id: uuid.UUID) -> Resource:
    """v1 has one default resource per business: the first active one by (name, id)."""
    resource = await session.scalar(
        select(Resource)
        .where(Resource.business_id == business_id, Resource.is_active)
        .order_by(Resource.name, Resource.id)
        .limit(1)
    )
    if resource is None:
        raise NotFound("No active resource")
    return resource


async def get_or_create_today_queue(
    session: AsyncSession, business: Business, now: datetime
) -> Queue:
    """Today's queue in the business time zone, created on first access. Never commits, so it
    can run inside the caller's transaction. A paused or closed queue is returned as it is."""
    resource = await default_resource(session, business.id)
    queue_date = now.astimezone(get_zone(business.timezone)).date()
    # No check-then-insert: a concurrent insert makes this one wait, then do nothing.
    await session.execute(
        insert(Queue)
        .values(
            business_id=business.id,
            resource_id=resource.id,
            queue_date=queue_date,
            status="open",
        )
        .on_conflict_do_nothing(index_elements=[Queue.resource_id, Queue.queue_date])
    )
    # ON CONFLICT DO NOTHING returns no row on a conflict, so read it in a new statement.
    return (
        await session.scalars(
            select(Queue).where(Queue.resource_id == resource.id, Queue.queue_date == queue_date)
        )
    ).one()


async def open_today_queue(
    session: AsyncSession, business_id: uuid.UUID, now: datetime
) -> QueueOut:
    business = await get_business(session, business_id)
    queue = await get_or_create_today_queue(session, business, now)
    await session.commit()
    return QueueOut.model_validate(queue)


def _count(status: str) -> ScalarSelect[int]:
    return (
        select(func.count())
        .where(QueueToken.queue_id == Queue.id, QueueToken.status == status)
        .correlate(Queue)
        .scalar_subquery()
    )


def _local(moment: datetime | None, zone: ZoneInfo) -> str | None:
    return None if moment is None else moment.astimezone(zone).isoformat()


@dataclass(frozen=True)
class MemberQueue:
    queue: Queue
    zone: ZoneInfo
    counts: SnapshotCounts


async def find_member_queue(
    session: AsyncSession, queue_id: uuid.UUID, user_id: uuid.UUID
) -> MemberQueue:
    """Owner or staff of the queue's business only. Anyone else gets the same 404 as an unknown
    id, because queue ids are not public. One statement: queue, membership and today's counts."""
    member = exists().where(
        BusinessMember.business_id == Queue.business_id,
        BusinessMember.user_id == user_id,
        BusinessMember.role.in_(("owner", "staff")),
    )
    row = (
        await session.execute(
            select(
                Queue,
                Business.timezone,
                _count("completed").label("completed"),
                _count("no_show").label("no_show"),
                _count("cancelled").label("cancelled"),
            )
            .join(Business, Business.id == Queue.business_id)
            .where(Queue.id == queue_id, member)
        )
    ).one_or_none()
    if row is None:
        raise NotFound("Queue not found")
    counts = SnapshotCounts(completed=row.completed, no_show=row.no_show, cancelled=row.cancelled)
    return MemberQueue(row.Queue, get_zone(row.timezone), counts)


async def get_snapshot(session: AsyncSession, found: MemberQueue) -> SnapshotOut:
    """One statement: the active tokens in the base order, joined to their customers."""
    zone, queue = found.zone, found.queue
    tokens = await session.execute(
        select(QueueToken, User.name)
        .join(User, User.id == QueueToken.customer_id)
        .where(QueueToken.queue_id == queue.id, QueueToken.status.in_(ACTIVE_TOKEN_STATUSES))
        .order_by(QueueToken.priority.desc(), QueueToken.queued_at, QueueToken.id)
    )
    items: list[SnapshotToken] = []
    waiting = 0
    for token, name in tokens.all():
        waiting += token.status == "waiting"
        items.append(
            SnapshotToken(
                id=token.id,
                token_no=token.token_no,
                status=token.status,
                priority=token.priority,
                source=token.source,
                skip_count=token.skip_count,
                version=token.version,
                joined_at=token.joined_at.astimezone(zone).isoformat(),
                queued_at=token.queued_at.astimezone(zone).isoformat(),
                called_at=_local(token.called_at, zone),
                started_at=_local(token.started_at, zone),
                order_position=waiting if token.status == "waiting" else None,
                appointment_id=token.appointment_id,
                customer=SnapshotCustomer(id=token.customer_id, name=name),
            )
        )
    return SnapshotOut(
        id=queue.id,
        business_id=queue.business_id,
        resource_id=queue.resource_id,
        queue_date=queue.queue_date,
        status=queue.status,
        counts=found.counts,
        tokens=items,
    )
