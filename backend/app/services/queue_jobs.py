"""Worker jobs on the queue: no-show sweeper, priority promotion, end of day.

Each job picks at most BATCH due tokens in a fixed order and changes each in its own
transaction through the same conditional UPDATE the staff actions use, so a racing staff
action or an overlapping run makes the job's update match nothing (counted as skipped). The
actor of their audit rows is None: the system.
"""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime

from sqlalchemy import Integer, and_, cast, exists, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import DomainError
from app.core.timezones import get_zone
from app.models import AuditLog, Business, Queue, QueueToken
from app.realtime.outbox import emit_token_event
from app.services.audit import record_audit
from app.services.queue import apply_event, emit_queue_status

logger = logging.getLogger(__name__)

BATCH = 200
Sessions = async_sessionmaker[AsyncSession]


@dataclass
class Summary:
    processed: int = 0
    changed: int = 0
    skipped: int = 0
    failed: int = 0
    duration_ms: int = 0


async def _each(
    job: str,
    sessions: Sessions,
    ids: list[tuple[uuid.UUID, int]],
    change: Callable[[AsyncSession, uuid.UUID, int], Awaitable[bool]],
) -> Summary:
    """One transaction per token. A lost race is skipped; a failing row is logged with its
    token id and error class only, rolled back, and does not stop the others."""
    started = time.perf_counter()
    summary = Summary()
    for token_id, version in ids:
        summary.processed += 1
        async with sessions() as session:
            try:
                if await change(session, token_id, version):
                    summary.changed += 1
                else:
                    summary.skipped += 1
            except DomainError:
                summary.skipped += 1  # a racing change won (version or state moved)
            except SQLAlchemyError as error:
                await session.rollback()
                summary.failed += 1
                logger.warning(
                    "%s failed token_id=%s error=%s", job, token_id, type(error).__name__
                )
    summary.duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info("%s %s", job, asdict(summary))
    return summary


def _minutes(key: str) -> object:
    return func.make_interval(0, 0, 0, 0, 0, cast(Business.settings[key].astext, Integer))


async def sweep_no_shows(sessions: Sessions, now: datetime, batch: int = BATCH) -> Summary:
    """FR-13, D29: called tokens whose called_at + grace_period_min has passed become no-shows,
    through the same path as a staff no-show."""
    async with sessions() as session:
        due = (
            await session.execute(
                select(QueueToken.id, QueueToken.version)
                .join(Business, Business.id == QueueToken.business_id)
                .where(
                    QueueToken.status == "called",
                    QueueToken.called_at + _minutes("grace_period_min") <= now,
                )
                .order_by(QueueToken.called_at, QueueToken.token_no)
                .limit(batch)
            )
        ).all()

    async def no_show(session: AsyncSession, token_id: uuid.UUID, version: int) -> bool:
        await apply_event(
            session, token_id, None, "mark_no_show", version, now, reason="grace_expired"
        )
        return True

    return await _each("no_show_sweeper", sessions, [(row.id, row.version) for row in due], no_show)


async def promote_waiting(sessions: Sessions, now: datetime, batch: int = BATCH) -> Summary:
    """D25: waiting priority-0 tokens whose wait (now - joined_at; a skip does not reset it)
    exceeds promotion_wait_limit_min go to priority 1, once in a token's life: a promoted token
    that staff lower to 0 again is not promoted a second time."""
    promoted = exists().where(
        AuditLog.business_id == QueueToken.business_id,
        AuditLog.created_at >= QueueToken.joined_at,  # bounded to the token's lifetime
        AuditLog.entity_id == QueueToken.id,
        AuditLog.action == "token.promote",
    )
    async with sessions() as session:
        due = (
            await session.execute(
                select(QueueToken.id, QueueToken.version)
                .join(Business, Business.id == QueueToken.business_id)
                .where(
                    QueueToken.status == "waiting",
                    QueueToken.priority == 0,
                    QueueToken.joined_at + _minutes("promotion_wait_limit_min") < now,
                    ~promoted,
                )
                .order_by(QueueToken.joined_at, QueueToken.token_no)
                .limit(batch)
            )
        ).all()

    async def promote(session: AsyncSession, token_id: uuid.UUID, version: int) -> bool:
        moved = (
            await session.execute(
                update(QueueToken)
                .where(
                    QueueToken.id == token_id,
                    QueueToken.version == version,
                    QueueToken.status == "waiting",
                    QueueToken.priority == 0,
                )
                .values(priority=1, version=version + 1)
                .returning(QueueToken.business_id, QueueToken.token_no)
            )
        ).one_or_none()
        if moved is None:  # staff changed it, or it was called or left, since the selection
            await session.rollback()
            return False
        record_audit(
            session,
            business_id=moved.business_id,
            actor_id=None,
            action="token.promote",
            entity_type="queue_token",
            entity_id=token_id,
            data={"token_no": moved.token_no, "from": 0, "to": 1},
            created_at=now,
        )
        await emit_token_event(session, token_id, "token.priority_changed", now, reason="promotion")
        await session.commit()
        return True

    return await _each("promotion", sessions, [(row.id, row.version) for row in due], promote)


async def end_of_day(sessions: Sessions, now: datetime, batch: int = BATCH) -> Summary:
    """D27: in every queue whose business-local day has ended, cancel waiting and called tokens
    (their appointments become cancelled, with no no-show counted: not the customer's fault),
    then close the queue. In-service tokens are left for staff to complete. Comparing dates
    rather than looking for a midnight instant also handles DST days and catches up after an
    outage of several days."""
    started = time.perf_counter()
    open_tokens = exists().where(
        QueueToken.queue_id == Queue.id, QueueToken.status.in_(("waiting", "called"))
    )
    async with sessions() as session:
        # No zone is ahead of UTC by a whole day, so an ended local day is never after today (UTC).
        candidates = (
            await session.execute(
                select(Queue.id, Queue.queue_date, Business.timezone)
                .join(Business, Business.id == Queue.business_id)
                .where(Queue.queue_date <= now.date(), or_(Queue.status != "closed", open_tokens))
                .order_by(Queue.queue_date, Queue.id)
            )
        ).all()
    ended = [row.id for row in candidates if _day_ended(row.queue_date, row.timezone, now)]
    async with sessions() as session:
        due = (
            await session.execute(
                select(QueueToken.id, QueueToken.version)
                .where(QueueToken.queue_id.in_(ended), QueueToken.status.in_(("waiting", "called")))
                .order_by(QueueToken.queue_id, QueueToken.token_no)
                .limit(batch)
            )
        ).all()

    async def cancel(session: AsyncSession, token_id: uuid.UUID, version: int) -> bool:
        await apply_event(
            session,
            token_id,
            None,
            "cancel",
            version,
            now,
            action="token.end_of_day",
            reason="end_of_day",
        )
        return True

    summary = await _each("end_of_day", sessions, [(row.id, row.version) for row in due], cancel)
    for queue_id in ended:
        async with sessions() as session:
            summary.changed += await _close(session, queue_id, now)
    summary.duration_ms = int((time.perf_counter() - started) * 1000)
    return summary


def _day_ended(queue_date: date, timezone: str, now: datetime) -> bool:
    return queue_date < now.astimezone(get_zone(timezone)).date()


async def _close(session: AsyncSession, queue_id: uuid.UUID, now: datetime) -> int:
    """Closes the queue once no waiting or called token is left in it (1 if it changed)."""
    previous = await session.scalar(select(Queue.status).where(Queue.id == queue_id))
    closed = await session.scalar(
        update(Queue)
        .where(
            Queue.id == queue_id,
            Queue.status != "closed",
            ~exists().where(
                and_(QueueToken.queue_id == queue_id, QueueToken.status.in_(("waiting", "called")))
            ),
        )
        .values(status="closed")
        .returning(Queue)
    )
    if closed is None:
        return 0
    await emit_queue_status(session, closed, str(previous), "closed", now, reason="end_of_day")
    record_audit(
        session,
        business_id=closed.business_id,
        actor_id=None,
        action="queue.close",
        entity_type="queue",
        entity_id=queue_id,
        data={"reason": "end_of_day"},
        created_at=now,
    )
    await session.commit()
    return 1
