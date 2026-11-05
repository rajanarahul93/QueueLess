import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import (
    ColumnElement,
    DateTime,
    Row,
    ScalarSelect,
    Select,
    String,
    UnaryExpression,
    and_,
    bindparam,
    exists,
    func,
    literal,
    select,
    true,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    AlreadyInQueue,
    InvalidTransition,
    NotFound,
    QueueClosed,
    ValidationFailed,
    VersionConflict,
    WalkInsFull,
)
from app.core.security import create_stream_token
from app.core.timezones import get_zone
from app.db.errors import driver_error
from app.models import (
    Appointment,
    Business,
    BusinessMember,
    OutboxEvent,
    Queue,
    QueueToken,
    Resource,
    Service,
    ServiceSession,
    User,
)
from app.realtime.outbox import emit_event, emit_token_event, lock_queue_events, with_service
from app.schemas.queue import (
    ActiveTokenOut,
    EtaOut,
    QueueOut,
    SnapshotCounts,
    SnapshotCustomer,
    SnapshotOut,
    SnapshotToken,
    StreamTokenOut,
    TokenOut,
)
from app.services.appointments import visible_to
from app.services.audit import record_audit
from app.services.business import get_business
from app.services.eta import (
    LAST_SESSIONS,
    MAX_SESSION_FACTOR,
    MIN_SESSION_SEC,
    Duration,
    Eta,
    expected,
    queue_etas,
    remaining,
)
from app.services.transitions import Event, transition

ACTIVE_TOKEN_STATUSES = ("waiting", "called", "in_service")

logger = logging.getLogger(__name__)


# The one ordering definition, shared by the snapshot, ahead_count and call-next. token_no is
# the final tiebreak: gap-free and assigned in join order under the queue lock.
def base_order() -> tuple[UnaryExpression[Any], ...]:  # Any: the terms have mixed column types
    return (QueueToken.priority.desc(), QueueToken.queued_at.asc(), QueueToken.token_no.asc())


class Orderable(Protocol):
    """What the ordering reads: a stored token, or one being added."""

    @property
    def priority(self) -> int: ...
    @property
    def source(self) -> str: ...
    @property
    def queued_at(self) -> datetime: ...
    @property
    def token_no(self) -> int: ...


def base_key(token: Orderable) -> tuple[int, datetime, int]:
    """base_order() in Python, for a token that is not in the database yet."""
    return (-token.priority, token.queued_at, token.token_no)


def walk_in_turn(history: Sequence[str], walk_ins: int, appointments: int) -> bool:
    """D22, generalised to a:b. `history` holds the sources of the most recent calls, newest
    first. After `appointments` consecutive appointment calls, up to `walk_ins` walk-ins are
    served in a row. The caller also requires a waiting walk-in and no waiting priority-2 token."""
    served = 0
    while served < len(history) and history[served] == "walk_in":
        served += 1
    if served >= walk_ins:
        return False
    before = history[served : served + appointments]
    return len(before) == appointments and all(source == "appointment" for source in before)


def call_order[T: Orderable](
    waiting: Sequence[T], history: Sequence[str], walk_ins: int, appointments: int
) -> list[T]:
    """The order call-next would follow, simulated with walk_in_turn() over waiting tokens given
    in base_order(). Later priority changes or promotions can still reorder it."""
    remaining, recent, ordered = list(waiting), list(history), list[T]()
    while remaining:
        pick = remaining[0]
        if walk_in_turn(recent, walk_ins, appointments) and not any(
            token.priority == 2 for token in remaining
        ):
            walk = [token for token in remaining if token.source == "walk_in"]
            if walk:
                pick = min(walk, key=lambda token: (token.queued_at, token.token_no))
        remaining.remove(pick)
        ordered.append(pick)
        # walk_in_turn() reads only this many recent calls; a longer list costs O(n) a pick
        recent = [pick.source, *recent][: walk_ins + appointments]
    return ordered


def history_query(queue_id: uuid.UUID, size: int) -> Select[str, datetime | None]:
    """Source and called_at of the most recent calls in a queue, newest first. A skip clears
    called_at, so a skipped token drops out until it is called again."""
    return (
        select(QueueToken.source, QueueToken.called_at)
        .where(QueueToken.queue_id == queue_id, QueueToken.called_at.is_not(None))
        .order_by(QueueToken.called_at.desc(), QueueToken.token_no.desc())
        .limit(size)
    )


def ratio(settings: Mapping[str, Any]) -> tuple[int, int]:  # Any: jsonb values
    value = settings["walk_in_ratio"]
    return int(value["walk_ins"]), int(value["appointments"])


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
    walk_in_ratio: tuple[int, int]


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
                Business.settings,
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
    return MemberQueue(row.Queue, get_zone(row.timezone), counts, ratio(row.settings))


def active_tokens_query(queue_id: uuid.UUID) -> Select[*tuple[Any, ...]]:
    """The snapshot's token read: active tokens in base_order(), with the customer's name and
    the token's service. One statement for the whole queue."""
    return with_service(
        select(QueueToken, User.name)
        .join(User, User.id == QueueToken.customer_id)
        .where(QueueToken.queue_id == queue_id, QueueToken.status.in_(ACTIVE_TOKEN_STATUSES))
        .order_by(*base_order())
    )


def next_waiting_query(queue_id: uuid.UUID) -> Select[QueueToken]:
    """Call-next's pick. The status predicate is re-checked on the locked row, so a token
    cancelled after the scan is skipped rather than called."""
    return (
        select(QueueToken)
        .where(QueueToken.queue_id == queue_id, QueueToken.status == "waiting")
        .order_by(*base_order())
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def _staff_token(
    token: QueueToken,
    name: str,
    zone: ZoneInfo,
    order_position: int | None,
    service: tuple[uuid.UUID | None, str | None],
) -> SnapshotToken:
    """The staff view of a token: the customer's id and name only."""
    return SnapshotToken(
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
        order_position=order_position,
        appointment_id=token.appointment_id,
        service_id=service[0],
        service_name=service[1],
        customer=SnapshotCustomer(id=token.customer_id, name=name),
    )


async def get_snapshot(session: AsyncSession, found: MemberQueue) -> SnapshotOut:
    """One statement: the active tokens in the base order, joined to their customers."""
    zone, queue = found.zone, found.queue
    rows = (await session.execute(active_tokens_query(queue.id))).all()
    walk_ins, appointments = found.walk_in_ratio
    recent = (await session.execute(history_query(queue.id, walk_ins + appointments))).all()
    history = [source for source, _ in recent]
    waiting = [row[0] for row in rows if row[0].status == "waiting"]
    order = {
        token.id: place
        for place, token in enumerate(call_order(waiting, history, walk_ins, appointments), 1)
    }
    items = [
        _staff_token(token, name, zone, order.get(token.id), (service_id, service_name))
        for token, name, service_id, service_name in rows
    ]
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


# --- positions and ETAs (FR-14, FR-15): one computation per queue, in call order ---

RESOURCES_PER_QUEUE = 1  # a queue belongs to one resource in v1; the ETA divides by this
SAMPLE_LOOKBACK = timedelta(days=90)  # bounds the hour-band search on a service's sessions


@dataclass(frozen=True)
class Position:
    token_no: int
    status: str
    ahead_count: int | None  # waiting tokens call-next serves first; null unless waiting
    eta: Eta | None  # null unless waiting


@dataclass(frozen=True)
class Ticket:
    """An active token as the ETA reads it: plain values, far cheaper than ORM objects when a
    join reads a long queue."""

    id: uuid.UUID
    status: str
    source: str
    priority: int
    queued_at: datetime
    token_no: int
    started_at: datetime | None


@dataclass(frozen=True)
class QueueData:
    """What a queue's positions and ETAs are computed from (gather() reads it in 5 queries)."""

    tokens: list[Ticket]  # active, in base_order()
    durations: dict[uuid.UUID, Duration]  # expected duration of each active token
    by_service: dict[uuid.UUID, Duration]
    walk_in: Duration  # a walk-in token that has no service yet
    remaining_sec: float  # time left on the sessions in progress
    history: list[str]
    walk_in_ratio: tuple[int, int]


def _samples_query() -> Any:  # Any: a UNION ALL of three LATERAL reads
    """Per service: its last sessions, its last sessions in this local hour, and today's, as
    (kind, service_id, duration_sec, ended_at). Each read walks the (service_id, ended_at)
    index. Implausible sessions never count. Parameters: business_id, zone, hour, day_start,
    since."""

    def sessions(*conditions: ColumnElement[bool], limit: int | None) -> Any:  # Any: LATERAL
        return (
            select(ServiceSession.duration_sec, ServiceSession.ended_at)
            .where(
                ServiceSession.service_id == Service.id,
                ServiceSession.duration_sec.between(
                    MIN_SESSION_SEC, Service.duration_min * 60 * MAX_SESSION_FACTOR
                ),
                *conditions,
            )
            .order_by(ServiceSession.ended_at.desc())
            .limit(limit)
            .lateral()
        )

    local_hour = func.extract(
        "hour", func.timezone(bindparam("zone", type_=String), ServiceSession.started_at)
    )
    since = ServiceSession.ended_at > bindparam("since", type_=DateTime(timezone=True))
    today = ServiceSession.started_at >= bindparam("day_start", type_=DateTime(timezone=True))
    reads = {
        "recent": sessions(limit=LAST_SESSIONS),
        "band": sessions(local_hour == bindparam("hour"), since, limit=LAST_SESSIONS),
        "today": sessions(today, limit=None),
    }
    return union_all(
        *(
            select(literal(kind).label("kind"), Service.id, read.c.duration_sec, read.c.ended_at)
            .select_from(Service)
            .join(read, true())
            .where(Service.business_id == bindparam("business_id"))
            for kind, read in reads.items()
        )
    )


SAMPLES = _samples_query()  # built once: a join runs it on every call


async def _durations(
    session: AsyncSession, business_id: uuid.UUID, zone_name: str, now: datetime
) -> tuple[dict[uuid.UUID, Duration], Duration]:
    """Expected duration per service, and for a walk-in without a service: the business's
    last sessions of any service, else the mean configured duration of its active services."""
    local = now.astimezone(get_zone(zone_name))
    day_start = datetime.combine(local.date(), time.min, local.tzinfo)
    services = (
        await session.execute(
            select(Service.id, Service.duration_min, Service.is_active).where(
                Service.business_id == business_id
            )
        )
    ).all()
    samples: dict[tuple[str, uuid.UUID], list[float]] = defaultdict(list)
    recent: list[tuple[datetime, float]] = []
    values = {
        "business_id": business_id,
        "zone": zone_name,
        "hour": local.hour,
        "day_start": day_start,
        "since": now - SAMPLE_LOOKBACK,
    }
    for kind, service_id, duration_sec, ended_at in (await session.execute(SAMPLES, values)).all():
        samples[kind, service_id].append(duration_sec)
        if kind == "recent":
            recent.append((ended_at, duration_sec))
    by_service = {
        row.id: expected(
            [samples["band", row.id], samples["recent", row.id]],
            row.duration_min * 60,
            samples["today", row.id],
        )
        for row in services
    }
    # The business's last sessions are among each service's last ones, so this is exact.
    latest = [duration for _, duration in sorted(recent, reverse=True)[:LAST_SESSIONS]]
    active = [row.duration_min * 60 for row in services if row.is_active]
    configured = sum(active) / len(active) if active else 0.0
    today = [value for (kind, _), values in samples.items() if kind == "today" for value in values]
    return by_service, expected([latest], configured, today)


async def gather(session: AsyncSession, queue_id: uuid.UUID, now: datetime) -> QueueData | None:
    """None for an unknown queue."""
    found = (
        await session.execute(
            select(Queue.business_id, Business.settings, Business.timezone)
            .join(Business, Business.id == Queue.business_id)
            .where(Queue.id == queue_id)
        )
    ).one_or_none()
    if found is None:
        return None
    rows = (
        await session.execute(
            select(
                QueueToken.id,
                QueueToken.status,
                QueueToken.source,
                QueueToken.priority,
                QueueToken.queued_at,
                QueueToken.token_no,
                QueueToken.started_at,
                Appointment.service_id,
                ServiceSession.service_id,
            )
            .outerjoin(Appointment, Appointment.id == QueueToken.appointment_id)
            .outerjoin(
                ServiceSession,
                and_(ServiceSession.token_id == QueueToken.id, ServiceSession.ended_at.is_(None)),
            )
            .where(QueueToken.queue_id == queue_id, QueueToken.status.in_(ACTIVE_TOKEN_STATUSES))
            .order_by(*base_order())
        )
    ).all()
    walk_ins, appointments = ratio(found.settings)
    recent = (await session.execute(history_query(queue_id, walk_ins + appointments))).all()
    by_service, walk_in = await _durations(session, found.business_id, found.timezone, now)
    tokens = [Ticket(*row[:7]) for row in rows]
    durations, left = {}, 0.0
    for token, (*_, booked_service, session_service) in zip(tokens, rows, strict=True):
        service_id = session_service or booked_service
        durations[token.id] = by_service[service_id] if service_id else walk_in
        if token.status == "in_service" and token.started_at is not None:
            elapsed = (now - token.started_at).total_seconds()
            left += remaining(durations[token.id].mean, elapsed)
    return QueueData(
        tokens=tokens,
        durations=durations,
        by_service=by_service,
        walk_in=walk_in,
        remaining_sec=left,
        history=[source for source, _ in recent],
        walk_in_ratio=(walk_ins, appointments),
    )


def _in_call_order(data: QueueData, extra: Ticket | None = None) -> list[Ticket]:
    """The waiting tokens (and `extra`, a token being added) in the order call-next follows."""
    waiting = [token for token in data.tokens if token.status == "waiting"]
    if extra is not None:
        waiting.append(extra)
    return call_order(sorted(waiting, key=base_key), data.history, *data.walk_in_ratio)


def _called(data: QueueData) -> list[Duration]:
    return [data.durations[token.id] for token in data.tokens if token.status == "called"]


def positions(data: QueueData) -> dict[uuid.UUID, Position]:
    """Every active token's place and ETA."""
    order = _in_call_order(data)
    etas = queue_etas(
        data.remaining_sec,
        _called(data),
        [(token.id, data.durations[token.id]) for token in order],
        RESOURCES_PER_QUEUE,
    )
    found = {
        token.id: Position(token.token_no, token.status, None, None)
        for token in data.tokens
        if token.status != "waiting"
    }
    for place, token in enumerate(order):
        found[token.id] = Position(token.token_no, "waiting", place, etas[token.id])
    return found


async def queue_positions(
    session: AsyncSession, queue_id: uuid.UUID, now: datetime
) -> dict[uuid.UUID, Position]:
    data = await gather(session, queue_id, now)
    return positions(data) if data is not None else {}


async def cached_positions(
    session: AsyncSession, redis: Redis, queue_id: uuid.UUID, now: datetime, ttl_sec: int
) -> dict[uuid.UUID, Position]:
    """Cached for a few seconds under the queue's newest event id. Every change of a queue
    writes an event with a higher id, so a changed queue never reads an older entry. A queue
    without events (or whose events retention removed, a week later) uses 0; the TTL ends it.
    Redis down: computed from the database every time."""
    newest = await session.scalar(
        select(func.max(OutboxEvent.id)).where(OutboxEvent.queue_id == queue_id)
    )
    key = f"eta:{queue_id}:{newest or 0}"
    try:
        raw = await redis.get(key)
    except (RedisError, OSError):
        logger.warning("eta cache unavailable; computing from the database")
        raw = None
    if raw is not None:
        return {uuid.UUID(key): _position(value) for key, value in json.loads(raw).items()}
    found = await queue_positions(session, queue_id, now)
    try:
        await redis.set(key, json.dumps({str(k): _plain(v) for k, v in found.items()}), ex=ttl_sec)
    except (RedisError, OSError):
        logger.warning("eta cache unavailable; not stored")
    return found


def _plain(position: Position) -> list[Any]:  # Any: JSON values
    eta = position.eta
    times = [eta.min_minutes, eta.max_minutes, eta.wait_sec] if eta else None
    return [position.token_no, position.status, position.ahead_count, times]


def _position(value: list[Any]) -> Position:  # Any: JSON values
    token_no, status, ahead, times = value
    return Position(token_no, status, ahead, Eta(*times) if times else None)


async def call_order_of(
    session: AsyncSession, queue_id: uuid.UUID, settings: Mapping[str, Any]
) -> list[QueueToken]:  # Any: jsonb values
    """The waiting tokens in the order call-next will follow them."""
    waiting = (
        await session.scalars(
            select(QueueToken)
            .where(QueueToken.queue_id == queue_id, QueueToken.status == "waiting")
            .order_by(*base_order())
        )
    ).all()
    walk_ins, appointments = ratio(settings)
    recent = (await session.execute(history_query(queue_id, walk_ins + appointments))).all()
    return call_order(waiting, [source for source, _ in recent], walk_ins, appointments)


def _token_query() -> Select[QueueToken, date, str, str]:
    return (
        select(QueueToken, Queue.queue_date, Queue.status, Business.timezone)
        .join(Queue, Queue.id == QueueToken.queue_id)
        .join(Business, Business.id == QueueToken.business_id)
    )


def eta_out(position: Position | None) -> EtaOut | None:
    eta = position.eta if position is not None else None
    return EtaOut(min_minutes=eta.min_minutes, max_minutes=eta.max_minutes) if eta else None


def _token_out(row: Row[QueueToken, date, str, str], position: Position | None) -> ActiveTokenOut:
    token, queue_date, queue_status, timezone = row
    zone = get_zone(timezone)
    return ActiveTokenOut(
        id=token.id,
        queue_id=token.queue_id,
        token_no=token.token_no,
        status=token.status,
        source=token.source,
        queued_at=token.queued_at.astimezone(zone).isoformat(),
        joined_at=token.joined_at.astimezone(zone).isoformat(),
        version=token.version,
        ahead_count=position.ahead_count if position is not None else None,
        eta=eta_out(position),
        business_id=token.business_id,
        queue_date=queue_date,
        queue_status=queue_status,
    )


async def board(session: AsyncSession, queue_id: uuid.UUID) -> dict[str, list[int]]:
    """The public board: token numbers only (FR-35). Now serving, and the next five in the
    order call-next will follow."""
    settings = await session.scalar(
        select(Business.settings)
        .join(Queue, Queue.business_id == Business.id)
        .where(Queue.id == queue_id)
    )
    tokens = list(
        (
            await session.scalars(
                select(QueueToken)
                .where(
                    QueueToken.queue_id == queue_id,
                    QueueToken.status.in_(ACTIVE_TOKEN_STATUSES),
                )
                .order_by(*base_order())
            )
        ).all()
    )
    walk_ins, appointments = ratio(
        settings or {"walk_in_ratio": {"walk_ins": 1, "appointments": 2}}
    )
    history = [
        source
        for source, _ in (
            await session.execute(history_query(queue_id, walk_ins + appointments))
        ).all()
    ]
    waiting = [token for token in tokens if token.status == "waiting"]
    upcoming = call_order(waiting, history, walk_ins, appointments)[:5]
    return {
        "now_serving": sorted(t.token_no for t in tokens if t.status in ("called", "in_service")),
        "next": [token.token_no for token in upcoming],
    }


async def own_token(
    session: AsyncSession,
    token_id: uuid.UUID,
    customer_id: uuid.UUID,
    redis: Redis,
    now: datetime,
    ttl_sec: int,
) -> TokenOut:
    """The customer's own token; anyone else's is the same 404 as a missing id."""
    found = (
        await session.execute(
            select(QueueToken.customer_id, QueueToken.queue_id).where(QueueToken.id == token_id)
        )
    ).one_or_none()
    if found is None or found.customer_id != customer_id:
        raise NotFound("Token not found")
    places = await cached_positions(session, redis, found.queue_id, now, ttl_sec)
    return await _read_token(session, token_id, places.get(token_id))


async def _read_token(
    session: AsyncSession, token_id: uuid.UUID, position: Position | None
) -> TokenOut:
    row = (await session.execute(_token_query().where(QueueToken.id == token_id))).one()
    return TokenOut.model_validate(_token_out(row, position).model_dump())


async def list_active_tokens(
    session: AsyncSession, customer_id: uuid.UUID, redis: Redis, now: datetime, ttl_sec: int
) -> list[ActiveTokenOut]:
    """The caller's own active tokens with their queue, position and ETA."""
    rows = (
        await session.execute(
            _token_query()
            .where(
                QueueToken.customer_id == customer_id,
                QueueToken.status.in_(ACTIVE_TOKEN_STATUSES),
            )
            .order_by(QueueToken.joined_at, QueueToken.id)
        )
    ).all()
    places: dict[uuid.UUID, Position] = {}
    for queue_id in {row[0].queue_id for row in rows}:  # one active token per queue
        places |= await cached_positions(session, redis, queue_id, now, ttl_sec)
    return [_token_out(row, places.get(row[0].id)) for row in rows]


def _joining(
    data: QueueData | None, token: Ticket, service_id: uuid.UUID | None
) -> Position | None:
    """A new token's place, from the queue as read before its lock plus this token. Joins in
    the same burst do not see each other, so their predictions can be slightly low."""
    if data is None:
        return None
    duration = data.by_service[service_id] if service_id else data.walk_in
    order = _in_call_order(data, token)
    place = order.index(token)
    ahead = _called(data) + [data.durations[waiting.id] for waiting in order[:place]]
    eta = queue_etas(data.remaining_sec, ahead, [(token.id, duration)], RESOURCES_PER_QUEUE)
    return Position(token.token_no, "waiting", place, eta[token.id])


def _predicted(position: Position | None) -> int | None:
    return position.eta.wait_sec if position is not None and position.eta is not None else None


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
    """Walk-in join. No-shows do not block walk-ins. The ETA inputs are read before the queue
    lock, so the lock is held no longer than before."""
    data = await gather(session, queue_id, now)
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
    values: dict[str, Any] = {  # Any: column values
        "source": "walk_in",
        "customer_id": customer_id,
        "status": "waiting",
        "priority": 0,
        "joined_at": now,
        "queued_at": now,
    }
    token_id = uuid.uuid4()
    new = Ticket(token_id, "waiting", "walk_in", 0, now, queue.last_token_no + 1, None)
    position = _joining(data, new, None)
    try:
        await _issue_token(
            session, queue, id=token_id, predicted_wait_sec=_predicted(position), **values
        )
    except _DuplicateToken:
        await session.rollback()  # the number goes back with the rest
        raise AlreadyInQueue("You already have a token in this queue") from None
    await emit_token_event(session, token_id, "token.joined", now)
    await session.commit()
    return await _read_token(session, token_id, position)


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
        return await _appointment_token(session, appointment.id, now), False
    if appointment.status != "booked":
        raise InvalidTransition(
            f"Cannot check in an appointment that is {appointment.status}", reason="not_booked"
        )
    start = appointment.during.lower
    assert start is not None  # stored ranges are always bounded
    if now < start - CHECK_IN_OPENS_BEFORE:
        raise InvalidTransition("Check-in opens 30 minutes before the slot", reason="too_early")
    if now >= start + timedelta(minutes=duration_min):  # the service time, not the buffer
        raise InvalidTransition("The slot has ended; take a walk-in token", reason="too_late")
    on_time = now <= start + timedelta(minutes=business.settings["grace_period_min"])
    # The slot's local day, not today's: a check-in just before midnight joins tomorrow's queue.
    day = start.astimezone(get_zone(business.timezone)).date()
    created = await get_or_create_queue(session, business.id, appointment.resource_id, day)
    data = await gather(session, created.id, now)  # before the queue lock, as in join
    queue, _ = await _lock_queue(session, created.id)
    _require_open(queue)
    values: dict[str, Any] = {  # Any: column values
        "source": "appointment",
        "appointment_id": appointment.id,
        "customer_id": appointment.customer_id,
        "status": "waiting",
        "priority": 0,
        "joined_at": now,
        "queued_at": start if on_time else now,
    }
    token_id = uuid.uuid4()
    queued_at = values["queued_at"]
    new = Ticket(token_id, "waiting", "appointment", 0, queued_at, queue.last_token_no + 1, None)
    position = _joining(data, new, appointment.service_id)
    try:
        await _issue_token(
            session, queue, id=token_id, predicted_wait_sec=_predicted(position), **values
        )
    except _DuplicateToken as duplicate:
        await session.rollback()
        if duplicate.constraint == ONE_TOKEN_PER_APPOINTMENT:
            return await _appointment_token(session, appointment_id, now), False
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
    await emit_token_event(session, token_id, "token.joined", now)
    await session.commit()
    return await _read_token(session, token_id, position), True


async def _appointment_token(
    session: AsyncSession, appointment_id: uuid.UUID, now: datetime
) -> TokenOut:
    found = (
        await session.execute(
            select(QueueToken.id, QueueToken.queue_id).where(
                QueueToken.appointment_id == appointment_id
            )
        )
    ).one_or_none()
    if found is None:
        raise InvalidTransition("The appointment has no token")
    places = await queue_positions(session, found.queue_id, now)
    return await _read_token(session, found.id, places.get(found.id))


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
    if token.appointment_id is not None:  # global lock order: the appointment before the token
        await session.execute(
            select(Appointment.id).where(Appointment.id == token.appointment_id).with_for_update()
        )
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
    await emit_token_event(session, token_id, "token.cancelled", now, reason="left")
    await session.commit()


async def set_queue_status(
    session: AsyncSession, queue_id: uuid.UUID, actor_id: uuid.UUID, status: str, now: datetime
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
        await emit_queue_status(session, queue, old, status, now)
        queue.status = status
    await session.commit()
    return QueueOut.model_validate(queue)


# --- staff actions: call-next, start, complete, skip, no-show, priority, stream token ---
# Every token change is a conditional UPDATE on the version the caller read, so a racing change
# (another staff member, or the customer leaving) leaves exactly one winner.


async def find_member_token(session: AsyncSession, token_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """404 unless the caller is owner or staff of the token's business (ids are not public)."""
    found = await session.scalar(
        select(QueueToken.id).where(
            QueueToken.id == token_id,
            exists().where(
                BusinessMember.business_id == QueueToken.business_id,
                BusinessMember.user_id == user_id,
                BusinessMember.role.in_(("owner", "staff")),
            ),
        )
    )
    if found is None:
        raise NotFound("Token not found")


async def staff_token(session: AsyncSession, token_id: uuid.UUID) -> SnapshotToken:
    """The token, its customer's name, and its place in call order while it waits."""
    row = (
        await session.execute(
            with_service(
                select(QueueToken, User.name, Business.timezone, Business.settings)
                .join(User, User.id == QueueToken.customer_id)
                .join(Business, Business.id == QueueToken.business_id)
                .where(QueueToken.id == token_id)
            ).execution_options(populate_existing=True)
        )
    ).one()
    token, name, timezone, settings, service_id, service_name = row
    place = None
    if token.status == "waiting":
        order = await call_order_of(session, token.queue_id, settings)
        place = [waiting.id for waiting in order].index(token.id) + 1
    return _staff_token(token, name, get_zone(timezone), place, (service_id, service_name))


async def call_next(
    session: AsyncSession, queue_id: uuid.UUID, actor_id: uuid.UUID, now: datetime
) -> SnapshotToken | None:
    """Calls the first waiting token in base_order(), or returns None when nobody waits. SKIP
    LOCKED lets concurrent calls take different tokens without waiting on each other; the
    queue row is not locked. Paused and closed queues may still be served (staff finish)."""
    queue = await session.get(Queue, queue_id)
    if queue is None:
        raise NotFound("Queue not found")
    business = await get_business(session, queue.business_id)
    if queue.queue_date != now.astimezone(get_zone(business.timezone)).date():
        raise QueueClosed("This is not today's queue")
    # The queue's event lock, taken FIRST here and held until commit, makes the ratio decision
    # and the pick one-at-a-time per queue. Every other writer of the queue takes it last, just
    # before its outbox row; this path then only takes token rows with SKIP LOCKED, which never
    # waits, so no lock cycle is possible.
    await lock_queue_events(session, queue_id)
    token, last_called = await _pick(session, queue_id, ratio(business.settings))
    if token is None:
        return None
    # Calls in a queue must be ordered as they were made, for the ratio history: `now` was
    # taken before waiting for the lock, so nudge it past the latest call if needed.
    if last_called is not None and now <= last_called:
        now = last_called + timedelta(microseconds=1)
    change = transition(token.status, "call", skip_count=token.skip_count, now=now)
    await session.execute(
        update(QueueToken)
        .where(QueueToken.id == token.id, QueueToken.version == token.version)
        .values(**change.values, version=token.version + 1)
    )
    _audit(session, token, actor_id, "token.call")
    await emit_token_event(session, token.id, "token.called", now)
    await session.commit()
    return await staff_token(session, token.id)


async def _pick(
    session: AsyncSession, queue_id: uuid.UUID, walk_in_ratio: tuple[int, int]
) -> tuple[QueueToken | None, datetime | None]:
    """The next token: the oldest waiting walk-in when it is a walk-in's turn (D22) and no
    priority-2 token waits; otherwise the base order. Also returns the latest call time. Run
    under the per-queue advisory lock."""
    walk_ins, appointments = walk_in_ratio
    recent = (await session.execute(history_query(queue_id, walk_ins + appointments))).all()
    history = [source for source, _ in recent]
    last_called = recent[0][1] if recent else None
    if walk_in_turn(history, walk_ins, appointments):
        urgent = await session.scalar(
            select(QueueToken.id)
            .where(
                QueueToken.queue_id == queue_id,
                QueueToken.status == "waiting",
                QueueToken.priority == 2,
            )
            .limit(1)
        )
        if urgent is None:
            walk_in = await session.scalar(
                select(QueueToken)
                .where(
                    QueueToken.queue_id == queue_id,
                    QueueToken.status == "waiting",
                    QueueToken.source == "walk_in",
                )
                .order_by(QueueToken.queued_at, QueueToken.token_no)
                .limit(1)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if walk_in is not None:
                return walk_in, last_called
    token = await session.scalar(
        next_waiting_query(queue_id).execution_options(populate_existing=True)
    )
    return token, last_called


def _audit(
    session: AsyncSession,
    token: QueueToken,
    actor_id: uuid.UUID | None,
    action: str,
    at: datetime | None = None,
    **data: object,
) -> None:
    record_audit(
        session,
        business_id=token.business_id,
        actor_id=actor_id,
        action=action,
        entity_type="queue_token",
        entity_id=token.id,
        data={"token_no": token.token_no, **data},
        created_at=at,
    )


async def _read_for_change(session: AsyncSession, token_id: uuid.UUID, version: int) -> QueueToken:
    """Unlocked read; a stale version is refused before the state is even looked at."""
    token = await session.scalar(
        select(QueueToken)
        .where(QueueToken.id == token_id)
        .execution_options(populate_existing=True)
    )
    if token is None:
        raise NotFound("Token not found")
    if token.version != version:
        raise VersionConflict("The token changed; reload and try again")
    return token


async def _apply(session: AsyncSession, token: QueueToken, values: Mapping[str, object]) -> None:
    """The conditional UPDATE. Zero rows means another change won since the read: roll back
    (nothing partial survives) and say which kind of conflict it was."""
    moved = await session.scalar(
        update(QueueToken)
        .where(
            QueueToken.id == token.id,
            QueueToken.version == token.version,
            QueueToken.status == token.status,
        )
        .values(**values, version=token.version + 1)
        .returning(QueueToken.id)
    )
    if moved is None:
        await session.rollback()
        raise VersionConflict("The token changed; reload and try again")


async def _start_service(
    session: AsyncSession, token: QueueToken, body_service_id: uuid.UUID | None
) -> uuid.UUID:
    """The service of the session: the appointment's, or the one staff chose for a walk-in."""
    if token.appointment_id is not None:
        if body_service_id is not None:
            raise ValidationFailed(["body", "service_id"], "Appointment tokens use their service")
        service_id = await session.scalar(
            select(Appointment.service_id).where(Appointment.id == token.appointment_id)
        )
        assert service_id is not None
        return service_id
    if body_service_id is None:
        raise ValidationFailed(["body", "service_id"], "Required for walk-in tokens")
    active = await session.scalar(
        select(Service.id).where(
            Service.id == body_service_id,
            Service.business_id == token.business_id,
            Service.is_active,
        )
    )
    if active is None:  # unknown, inactive or another business's: as when booking
        raise NotFound("Service not found")
    return active


async def act_on_token(
    session: AsyncSession,
    token_id: uuid.UUID,
    actor_id: uuid.UUID,
    event: Event,
    version: int,
    now: datetime,
    service_id: uuid.UUID | None = None,
) -> SnapshotToken:
    """A staff action (start, complete, skip, mark_no_show), returned in the staff shape."""
    await apply_event(session, token_id, actor_id, event, version, now, service_id)
    return await staff_token(session, token_id)


async def apply_event(
    session: AsyncSession,
    token_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    event: Event,
    version: int,
    now: datetime,
    service_id: uuid.UUID | None = None,
    *,
    action: str | None = None,
    reason: str | None = None,
) -> None:
    """The one path for staff actions and worker jobs (actor None = the system). Order:
    version, then state, then locks in the global order (customer row only when the outcome is
    a no-show, then the appointment), then the conditional token UPDATE, then the side effects
    and the audit row, in one transaction."""
    token = await _read_for_change(session, token_id, version)
    change = transition(token.status, event, skip_count=token.skip_count, now=now)
    session_service = await _start_service(session, token, service_id) if event == "start" else None
    if change.status == "no_show":
        await session.execute(
            select(User.id).where(User.id == token.customer_id).with_for_update(key_share=True)
        )
    if token.appointment_id is not None:
        await session.execute(
            select(Appointment.id).where(Appointment.id == token.appointment_id).with_for_update()
        )
    await _apply(session, token, change.values)
    await _side_effects(session, token, change.status, now, session_service)
    extra = {"reason": reason} if reason else {}
    at = now if actor_id is None else None  # job rows carry the job's clock
    _audit(session, token, actor_id, action or f"token.{event}", at, status=change.status, **extra)
    await emit_token_event(session, token.id, EVENT_TYPES[change.status], now, **extra)
    await session.commit()


# Event type for each status a staff action or job can produce.
EVENT_TYPES = {
    "in_service": "token.started",
    "completed": "token.completed",
    "waiting": "token.skipped",
    "no_show": "token.no_show",
    "cancelled": "token.cancelled",
}


async def emit_queue_status(
    session: AsyncSession, queue: Queue, old: str, new: str, now: datetime, **extra: object
) -> None:
    await emit_event(
        session,
        business_id=queue.business_id,
        queue_id=queue.id,
        event_type="queue.status_changed",
        payload={"queue_id": str(queue.id), "from": old, "to": new, **extra},
        at=now,
    )


APPOINTMENT_STATUS = {
    "in_service": "in_service",
    "completed": "completed",
    "no_show": "no_show",
    "cancelled": "cancelled",
}


async def _side_effects(
    session: AsyncSession,
    token: QueueToken,
    new_status: str,
    now: datetime,
    session_service: uuid.UUID | None,
) -> None:
    if new_status == "in_service":
        resource_id = await session.scalar(
            select(Queue.resource_id).where(Queue.id == token.queue_id)
        )
        session.add(
            ServiceSession(
                business_id=token.business_id,
                token_id=token.id,
                service_id=session_service,
                resource_id=resource_id,
                started_at=now,
            )
        )
    elif new_status == "completed":
        started = await session.scalar(
            select(ServiceSession.started_at).where(
                ServiceSession.token_id == token.id, ServiceSession.ended_at.is_(None)
            )
        )
        if started is not None:
            await session.execute(
                update(ServiceSession)
                .where(ServiceSession.token_id == token.id, ServiceSession.ended_at.is_(None))
                .values(ended_at=now, duration_sec=int((now - started).total_seconds()))
            )
    elif new_status == "no_show":
        # Atomic increment, exactly once: only one conditional UPDATE can make this token a no-show.
        await session.execute(
            update(User)
            .where(User.id == token.customer_id)
            .values(no_show_count=User.no_show_count + 1)
        )
    if token.appointment_id is not None and new_status in APPOINTMENT_STATUS:
        await session.execute(
            update(Appointment)
            .where(Appointment.id == token.appointment_id)
            .values(status=APPOINTMENT_STATUS[new_status])
        )


async def set_priority(
    session: AsyncSession,
    token_id: uuid.UUID,
    actor_id: uuid.UUID,
    priority: int,
    version: int,
    now: datetime,
) -> SnapshotToken:
    """Waiting tokens only; the same value is a no-op (no version bump, no audit row)."""
    token = await _read_for_change(session, token_id, version)
    if token.status != "waiting":
        raise InvalidTransition(f"Cannot change the priority of a token that is {token.status}")
    if token.priority != priority:
        await _apply(session, token, {"priority": priority})
        _audit(session, token, actor_id, "token.priority", old=token.priority, new=priority)
        await emit_token_event(session, token_id, "token.priority_changed", now)
        await session.commit()
    return await staff_token(session, token_id)


def issue_stream_token(
    settings: Settings, queue_id: uuid.UUID, user_id: uuid.UUID
) -> StreamTokenOut:
    """Issued with real time: the verifier checks expiry against real time."""
    return StreamTokenOut(
        stream_token=create_stream_token(settings, "queue", queue_id, user_id),
        expires_in=settings.stream_token_ttl_sec,
    )
