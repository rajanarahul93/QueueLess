"""Owner reports (FR-30, FR-31) from daily_stats rows: stored rows for finished days, live rows
for today and for any earlier day the rollup has not reached yet. Each day comes from exactly
one of the two, so nothing is counted twice."""

import uuid
from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailyStat, Queue
from app.schemas.report import NoShowOut, PeakHourOut, WaitTimeOut
from app.services.rollups import STAT_COLUMNS, day_stats

Stat = dict[str, Any]  # Any: one daily_stats row by column name


async def stat_rows(
    session: AsyncSession,
    business_id: uuid.UUID,
    zone_name: str,
    first: date,
    last: date,
    today: date,
) -> list[Stat]:
    columns = [getattr(DailyStat, name) for name in STAT_COLUMNS]
    stored = (
        await session.execute(
            select(*columns).where(
                DailyStat.business_id == business_id,
                DailyStat.stat_date.between(first, last),
                DailyStat.stat_date < today,  # today is always live
            )
        )
    ).all()
    found = [dict(row._mapping) for row in stored]
    rolled = {row["stat_date"] for row in found}
    live_days = (
        await session.scalars(
            select(Queue.queue_date)
            .where(
                Queue.business_id == business_id,
                Queue.queue_date.between(first, last),
                Queue.queue_date.not_in(rolled),
            )
            .distinct()
        )
    ).all()
    for day in sorted(live_days):
        live = await session.execute(day_stats(business_id, zone_name, day))
        found += [dict(row._mapping) for row in live.all()]
    return found


def peak_hours(rows: list[Stat]) -> list[PeakHourOut]:
    tokens: dict[tuple[int, int], int] = defaultdict(int)
    for row in rows:
        if row["hour"] is not None:
            tokens[row["stat_date"].weekday(), row["hour"]] += row["tokens_joined"]
    return [
        PeakHourOut(weekday=weekday, hour=hour, tokens=count)
        for (weekday, hour), count in sorted(tokens.items())
        if count
    ]


def wait_times(rows: list[Stat], service_id: uuid.UUID | None) -> list[WaitTimeOut]:
    days = [
        row
        for row in rows
        if row["hour"] is None
        and row["service_id"] is not None
        and row["avg_wait_sec"] is not None
        and service_id in (None, row["service_id"])
    ]
    return [
        WaitTimeOut(
            date=row["stat_date"],
            service_id=row["service_id"],
            served=row["tokens_served"],
            avg_wait_sec=row["avg_wait_sec"],
            p90_wait_sec=row["p90_wait_sec"],
            avg_predicted_wait_sec=row["avg_predicted_wait_sec"],
        )
        for row in sorted(days, key=lambda row: (row["stat_date"], str(row["service_id"])))
    ]


def no_shows(rows: list[Stat]) -> list[NoShowOut]:
    totals: dict[uuid.UUID | None, list[int]] = defaultdict(lambda: [0, 0, 0])
    for row in rows:
        if row["hour"] is None:
            total = totals[row["service_id"]]
            total[0] += row["tokens_joined"]
            total[1] += row["no_shows"]
            total[2] += row["cancelled"]
    return [
        NoShowOut(
            service_id=service_id,
            joined=joined,
            no_shows=missed,
            cancelled=cancelled,
            no_show_rate=round(missed / joined, 4),
            cancel_rate=round(cancelled / joined, 4),
        )
        for service_id, (joined, missed, cancelled) in sorted(
            totals.items(), key=lambda item: (item[0] is None, str(item[0]))
        )
        if joined
    ]
