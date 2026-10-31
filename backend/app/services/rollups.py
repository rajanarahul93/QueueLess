"""The nightly rollup into daily_stats (FR-31), and the one definition of its figures.

day_stats() is the only place the figures are defined: the rollup stores its rows for a
finished day and the reports run it live for today, so the two can never disagree.
A token belongs to its queue's day (the business-local date) and to the local hour of joined_at.
It counts under the service of its session, else of its appointment, else under no service
(service_id NULL: a walk-in that never started). Each service also gets a whole-day row
(hour NULL) with the day's exact average and p90 wait, since hourly p90s cannot be combined.

Definitions: tokens_joined = every token; tokens_served = completed with a session;
no_shows and cancelled = tokens in that status; wait = started_at - joined_at (never queued_at,
D4) over tokens that started; p90 = percentile_cont(0.9), from MIN_P90_SAMPLES waits;
avg_predicted_wait_sec = mean predicted_wait_sec over the same started tokens.
On a DST night the repeated local hour is one bucket and the skipped hour has none.
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import (
    Integer,
    SmallInteger,
    case,
    cast,
    delete,
    exists,
    func,
    insert,
    literal,
    select,
    tuple_,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.timezones import get_zone
from app.models import Appointment, Business, DailyStat, Queue, QueueToken, ServiceSession
from app.services.queue_jobs import Summary

logger = logging.getLogger(__name__)

MIN_P90_SAMPLES = 5
ROLLUP_DAYS = 3  # the last complete local days, recomputed on every run
ROLLUP_BUDGET_SEC = 40  # under the 50 s job timeout; what is left waits for the next hour
STAT_COLUMNS = [
    "business_id",
    "service_id",
    "stat_date",
    "hour",
    "tokens_joined",
    "tokens_served",
    "no_shows",
    "cancelled",
    "avg_wait_sec",
    "p90_wait_sec",
    "avg_predicted_wait_sec",
]


def _whole(value: Any) -> Any:  # Any: SQL expressions
    return cast(func.round(value), Integer)


def day_stats(business_id: uuid.UUID, zone_name: str, day: date) -> Any:  # Any: a Select
    """The rows of one business-local day, in STAT_COLUMNS order."""
    tokens = (
        select(
            func.coalesce(ServiceSession.service_id, Appointment.service_id).label("service_id"),
            cast(
                func.extract("hour", func.timezone(zone_name, QueueToken.joined_at)), SmallInteger
            ).label("hour"),
            QueueToken.status,
            func.extract("epoch", QueueToken.started_at - QueueToken.joined_at).label("wait"),
            QueueToken.predicted_wait_sec,
            ServiceSession.id.label("session_id"),
        )
        .select_from(QueueToken)
        .join(Queue, Queue.id == QueueToken.queue_id)
        .outerjoin(Appointment, Appointment.id == QueueToken.appointment_id)
        .outerjoin(ServiceSession, ServiceSession.token_id == QueueToken.id)
        .where(Queue.business_id == business_id, Queue.queue_date == day)
        .subquery()
    )
    wait = tokens.c.wait
    return select(
        literal(business_id).label("business_id"),
        tokens.c.service_id,
        literal(day).label("stat_date"),
        tokens.c.hour,
        func.count().label("tokens_joined"),
        func.count()
        .filter(tokens.c.status == "completed", tokens.c.session_id.is_not(None))
        .label("tokens_served"),
        func.count().filter(tokens.c.status == "no_show").label("no_shows"),
        func.count().filter(tokens.c.status == "cancelled").label("cancelled"),
        _whole(func.avg(wait)).label("avg_wait_sec"),
        case(
            (
                func.count(wait) >= MIN_P90_SAMPLES,
                _whole(func.percentile_cont(0.9).within_group(wait)),
            ),
            else_=None,
        ).label("p90_wait_sec"),
        _whole(func.avg(tokens.c.predicted_wait_sec).filter(wait.is_not(None))).label(
            "avg_predicted_wait_sec"
        ),
    ).group_by(
        func.grouping_sets(tuple_(tokens.c.service_id, tokens.c.hour), tuple_(tokens.c.service_id))
    )


async def roll_day(
    session: AsyncSession, business_id: uuid.UUID, zone_name: str, day: date
) -> None:
    """Rewrites one business-day in the caller's transaction: a reader sees the old rows or
    the new ones, never a part."""
    await session.execute(
        delete(DailyStat).where(DailyStat.business_id == business_id, DailyStat.stat_date == day)
    )
    await session.execute(
        insert(DailyStat).from_select(STAT_COLUMNS, day_stats(business_id, zone_name, day))
    )


async def _due(session: AsyncSession, now: datetime) -> list[tuple[uuid.UUID, date, str]]:
    """Business-days of the last ROLLUP_DAYS complete local days that had a queue; days never
    rolled first, then the oldest, so a run cut short by its budget is picked up next hour."""
    rolled = exists().where(
        DailyStat.business_id == Queue.business_id, DailyStat.stat_date == Queue.queue_date
    )
    rows = (
        await session.execute(
            select(Queue.business_id, Queue.queue_date, Business.timezone, rolled.label("rolled"))
            .join(Business, Business.id == Queue.business_id)
            .where(  # a superset in UTC dates; the local window is applied below
                Queue.queue_date >= now.date() - timedelta(days=ROLLUP_DAYS + 1),
                Queue.queue_date <= now.date() + timedelta(days=1),
            )
            .distinct()
        )
    ).all()
    due = []
    for business_id, day, zone_name, _ in sorted(rows, key=lambda row: (row[3], row[1])):
        today = now.astimezone(get_zone(zone_name)).date()
        if today - timedelta(days=ROLLUP_DAYS) <= day < today:
            due.append((business_id, day, zone_name))
    return due


async def rollup(
    sessions: async_sessionmaker[AsyncSession],
    now: datetime,
    budget_sec: float = ROLLUP_BUDGET_SEC,
    timer: Callable[[], float] = time.monotonic,
) -> Summary:
    """Hourly: recomputes every due business-day, one transaction each, until the budget is
    spent. summary.skipped counts the business-days left for the next run."""
    started = timer()
    summary = Summary()
    async with sessions() as session:
        due = await _due(session, now)
    for index, (business_id, day, zone_name) in enumerate(due):
        if timer() - started >= budget_sec:
            summary.skipped = len(due) - index
            break
        summary.processed += 1
        async with sessions() as session:
            try:
                await roll_day(session, business_id, zone_name, day)
                await session.commit()
                summary.changed += 1
            except SQLAlchemyError as error:
                await session.rollback()
                summary.failed += 1
                logger.warning(
                    "rollup failed business_id=%s day=%s error=%s",
                    business_id,
                    day,
                    type(error).__name__,
                )
    summary.duration_ms = int((timer() - started) * 1000)
    logger.info("rollup %s remaining=%d", asdict(summary), summary.skipped)
    return summary
