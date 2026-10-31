import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import (
    ColumnElement,
    Row,
    ScalarSelect,
    Select,
    and_,
    case,
    exists,
    func,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import (
    AlreadyInQueue,
    InvalidTransition,
    NotFound,
    QueueClosed,
    VersionConflict,
    WalkInsFull,
)
from app.core.timezones import get_zone
from app.db.errors import driver_error
from app.models import (
    Appointment,
    Business,
    BusinessMember,
    Queue,
    QueueToken,
    Resource,
    Service,
    User,
)
from app.schemas.queue import (
    ActiveTokenOut,
    QueueOut,
    SnapshotCounts,
    SnapshotCustomer,
    SnapshotOut,
    SnapshotToken,
    TokenOut,
)
from app.services.appointments import visible_to
from app.services.audit import record_audit
from app.services.business import get_business
from app.services.transitions import transition

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


async def get_or_create_queue(
    session: AsyncSession, business_id: uuid.UUID, resource_id: uuid.UUID, day: date
) -> Queue:
    """The queue of one resource and local day, created on first access. Never commits, so it can
    run inside the caller's transaction. A paused or closed queue is returned as it is."""
    # No check-then-insert: a concurrent insert makes this one wait, then do nothing.
    await session.execute(
        insert(Queue)
        .values(business_id=business_id, resource_id=resource_id, queue_date=day, status="open")
        .on_conflict_do_nothing(index_elements=[Queue.resource_id, Queue.queue_date])
    )
    # ON CONFLICT DO NOTHING returns no row on a conflict, so read it in a new statement.
    return (
        await session.scalars(
            select(Queue).where(Queue.resource_id == resource_id, Queue.queue_date == day)
        )
    ).one()


async def get_or_create_today_queue(
    session: AsyncSession, business: Business, now: datetime
) -> Queue:
    """Today's queue (in the business time zone) for the default resource."""
    resource = await default_resource(session, business.id)
    today = now.astimezone(get_zone(business.timezone)).date()
    return await get_or_create_queue(session, business.id, resource.id, today)


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


# --- tokens: join, check-in, leave, list; and queue status ---
# Global lock order, never taken backwards: customer user row, appointment row, queue row,
# token rows. Join takes the queue only; check-in the appointment then the queue.

ONE_ACTIVE_TOKEN = "one_active_token"
ONE_TOKEN_PER_APPOINTMENT = "uq_queue_tokens_appointment_id"
CHECK_IN_OPENS_BEFORE = timedelta(minutes=30)
QUEUE_MOVES = {("open", "paused"), ("paused", "open"), ("open", "closed"), ("paused", "closed")}


class _DuplicateToken(Exception):
    def __init__(self, constraint: str) -> None:
        self.constraint = constraint


def _ahead_count() -> ColumnElement[int | None]:
    """Waiting tokens ahead in the base order (priority DESC, queued_at, id); null when the
    token itself is not waiting."""
    other = aliased(QueueToken)
    ahead = (
        select(func.count())
        .where(
            other.queue_id == QueueToken.queue_id,
            other.status == "waiting",
            or_(
                other.priority > QueueToken.priority,
                and_(
                    other.priority == QueueToken.priority,
                    tuple_(other.queued_at, other.id) < tuple_(QueueToken.queued_at, QueueToken.id),
                ),
            ),
        )
        .correlate(QueueToken)
        .scalar_subquery()
    )
    return case((QueueToken.status == "waiting", ahead), else_=None)


def _token_query() -> Select[QueueToken, date, str, str, int | None]:
    return (
        select(QueueToken, Queue.queue_date, Queue.status, Business.timezone, _ahead_count())
        .join(Queue, Queue.id == QueueToken.queue_id)
        .join(Business, Business.id == QueueToken.business_id)
    )


def _token_out(row: Row[QueueToken, date, str, str, int | None]) -> ActiveTokenOut:
    token, queue_date, queue_status, timezone, ahead = row
    zone = get_zone(timezone)
    return ActiveTokenOut(
        id=token.id,
        queue_id=token.queue_id,
        token_no=token.token_no,
        status=token.status,
        priority=token.priority,
        source=token.source,
        queued_at=token.queued_at.astimezone(zone).isoformat(),
        joined_at=token.joined_at.astimezone(zone).isoformat(),
        version=token.version,
        ahead_count=ahead,
        business_id=token.business_id,
        queue_date=queue_date,
        queue_status=queue_status,
    )


async def _read_token(session: AsyncSession, token_id: uuid.UUID) -> TokenOut:
    row = (await session.execute(_token_query().where(QueueToken.id == token_id))).one()
    return TokenOut.model_validate(_token_out(row).model_dump())


async def list_active_tokens(session: AsyncSession, customer_id: uuid.UUID) -> list[ActiveTokenOut]:
    """One statement: the caller's own active tokens with their queue and position."""
    rows = await session.execute(
        _token_query()
        .where(
            QueueToken.customer_id == customer_id,
            QueueToken.status.in_(ACTIVE_TOKEN_STATUSES),
        )
        .order_by(QueueToken.joined_at, QueueToken.id)
    )
    return [_token_out(row) for row in rows.all()]


async def _issue_token(session: AsyncSession, queue: Queue, **values: object) -> uuid.UUID:
    """Takes the next number and inserts the token in the caller's transaction, so a failure
    rolls the number back too (gap-free). The queue row must already be locked."""
    token_no = (
        await session.execute(
            update(Queue)
            .where(Queue.id == queue.id)
            .values(last_token_no=Queue.last_token_no + 1)
            .returning(Queue.last_token_no)
        )
    ).scalar_one()
    try:
        return (
            await session.execute(
                insert(QueueToken)
                .values(
                    business_id=queue.business_id, queue_id=queue.id, token_no=token_no, **values
                )
                .returning(QueueToken.id)
            )
        ).scalar_one()
    except IntegrityError as error:
        _, constraint = driver_error(error)
        if constraint in (ONE_ACTIVE_TOKEN, ONE_TOKEN_PER_APPOINTMENT):
            raise _DuplicateToken(constraint) from error
        raise


async def _lock_queue(session: AsyncSession, queue_id: uuid.UUID) -> tuple[Queue, Business]:
    row = (
        await session.execute(
            select(Queue, Business)
            .join(Business, Business.id == Queue.business_id)
            .where(Queue.id == queue_id)
            .with_for_update(of=Queue)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise NotFound("Queue not found")
    return row.Queue, row.Business


def _require_open(queue: Queue) -> None:
    if queue.status != "open":
        raise QueueClosed(f"The queue is {queue.status}")


async def join_queue(
    session: AsyncSession, queue_id: uuid.UUID, customer_id: uuid.UUID, now: datetime
) -> TokenOut:
    """Walk-in join. No-shows do not block walk-ins."""
    queue, business = await _lock_queue(session, queue_id)
    _require_open(queue)
    if queue.queue_date != now.astimezone(get_zone(business.timezone)).date():
        raise QueueClosed("This is not today's queue")
    walk_ins = await session.scalar(
        select(func.count()).where(
            QueueToken.queue_id == queue.id,
            QueueToken.source == "walk_in",
            QueueToken.status != "cancelled",
        )
    )
    if (walk_ins or 0) >= business.settings["max_walk_ins"]:
        raise WalkInsFull("Walk-ins are full for today")
    try:
        token_id = await _issue_token(
            session,
            queue,
            source="walk_in",
            customer_id=customer_id,
            status="waiting",
            priority=0,
            joined_at=now,
            queued_at=now,
        )
    except _DuplicateToken:
        await session.rollback()  # the number goes back with the rest
        raise AlreadyInQueue("You already have a token in this queue") from None
    await session.commit()
    return await _read_token(session, token_id)


async def visible_appointment(
    session: AsyncSession, appointment_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    """404 unless the caller owns the appointment or is owner or staff of its business."""
    found = await session.scalar(
        select(Appointment.id).where(Appointment.id == appointment_id, visible_to(actor_id))
    )
    if found is None:
        raise NotFound("Appointment not found")


async def check_in(
    session: AsyncSession, appointment_id: uuid.UUID, actor_id: uuid.UUID, now: datetime
) -> tuple[TokenOut, bool]:
    """Returns the token and whether it was created now (a repeated check-in returns the same
    token). Locks the appointment, then the queue of the slot's local day."""
    row = (
        await session.execute(
            select(Appointment, Service.duration_min, Business)
            .join(Service, Service.id == Appointment.service_id)
            .join(Business, Business.id == Appointment.business_id)
            .where(Appointment.id == appointment_id, visible_to(actor_id))
            .with_for_update(of=Appointment)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise NotFound("Appointment not found")
    appointment, duration_min, business = row
    if appointment.status == "checked_in":
        return await _appointment_token(session, appointment.id), False
    if appointment.status != "booked":
        raise InvalidTransition(f"Cannot check in an appointment that is {appointment.status}")
    start = appointment.during.lower
    assert start is not None  # stored ranges are always bounded
    if now < start - CHECK_IN_OPENS_BEFORE:
        raise InvalidTransition("Check-in opens 30 minutes before the slot")
    if now >= start + timedelta(minutes=duration_min):  # the service time, not the buffer
        raise InvalidTransition("The slot has ended; take a walk-in token")
    on_time = now <= start + timedelta(minutes=business.settings["grace_period_min"])
    # The slot's local day, not today's: a check-in just before midnight joins tomorrow's queue.
    day = start.astimezone(get_zone(business.timezone)).date()
    created = await get_or_create_queue(session, business.id, appointment.resource_id, day)
    queue, _ = await _lock_queue(session, created.id)
    _require_open(queue)
    try:
        token_id = await _issue_token(
            session,
            queue,
            source="appointment",
            appointment_id=appointment.id,
            customer_id=appointment.customer_id,
            status="waiting",
            priority=0,
            joined_at=now,
            queued_at=start if on_time else now,
        )
    except _DuplicateToken as duplicate:
        await session.rollback()
        if duplicate.constraint == ONE_TOKEN_PER_APPOINTMENT:
            return await _appointment_token(session, appointment_id), False
        raise AlreadyInQueue("The customer already has a token in this queue") from None
    await session.execute(
        update(Appointment).where(Appointment.id == appointment.id).values(status="checked_in")
    )
    if actor_id != appointment.customer_id:
        record_audit(
            session,
            business_id=business.id,
            actor_id=actor_id,
            action="appointment.check_in",
            entity_type="appointment",
            entity_id=appointment.id,
            data={"token_id": str(token_id)},
        )
    await session.commit()
    return await _read_token(session, token_id), True


async def _appointment_token(session: AsyncSession, appointment_id: uuid.UUID) -> TokenOut:
    token_id = await session.scalar(
        select(QueueToken.id).where(QueueToken.appointment_id == appointment_id)
    )
    if token_id is None:
        raise InvalidTransition("The appointment has no token")
    return await _read_token(session, token_id)


async def leave_queue(
    session: AsyncSession, token_id: uuid.UUID, customer_id: uuid.UUID, now: datetime
) -> None:
    """Optimistic: the UPDATE only applies to the version that was read, so a racing change
    makes exactly one side win. Leaving an appointment token cancels the appointment."""
    token = await session.scalar(
        select(QueueToken).where(QueueToken.id == token_id, QueueToken.customer_id == customer_id)
    )
    if token is None:
        raise NotFound("Token not found")
    if token.status == "cancelled":
        return  # leaving twice is a no-op
    change = transition(token.status, "cancel", skip_count=token.skip_count, now=now)
    moved = (
        await session.execute(
            update(QueueToken)
            .where(QueueToken.id == token_id, QueueToken.version == token.version)
            .values(**change.values, version=token.version + 1)
            .returning(QueueToken.appointment_id)
        )
    ).one_or_none()
    if moved is None:
        await session.rollback()
        current = await session.scalar(select(QueueToken.status).where(QueueToken.id == token_id))
        if current == "cancelled":
            return
        raise VersionConflict("The token changed; reload and try again")
    if moved.appointment_id is not None:
        await session.execute(
            update(Appointment)
            .where(Appointment.id == moved.appointment_id)
            .values(status="cancelled")
        )
    await session.commit()


async def set_queue_status(
    session: AsyncSession, queue_id: uuid.UUID, actor_id: uuid.UUID, status: str
) -> QueueOut:
    """open <-> paused, open or paused -> closed; closed is final for the day."""
    queue, _ = await _lock_queue(session, queue_id)
    old = queue.status
    if old != status:
        if (old, status) not in QUEUE_MOVES:
            raise InvalidTransition(f"Cannot change a {old} queue to {status}")
        await session.execute(update(Queue).where(Queue.id == queue_id).values(status=status))
        record_audit(
            session,
            business_id=queue.business_id,
            actor_id=actor_id,
            action="queue.status",
            entity_type="queue",
            entity_id=queue_id,
            data={"from": old, "to": status},
        )
        queue.status = status
    await session.commit()
    return QueueOut.model_validate(queue)
