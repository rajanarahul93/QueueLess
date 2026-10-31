"""Retention (D14), run by the worker every 10 minutes in bounded batches.

Published outbox rows go after 7 days (unpublished rows are never deleted: they are not yet
delivered). Audit rows go after 12 months. Refresh tokens go once they expired more than 7 days
ago; revoked tokens that have not expired stay, because reuse detection needs them.
"""

import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditLog, OutboxEvent, RefreshToken
from app.services.queue_jobs import Summary

logger = logging.getLogger(__name__)

BATCH = 5000
OUTBOX_DAYS = 7
REFRESH_GRACE_DAYS = 7


def twelve_months_before(now: datetime) -> datetime:
    try:
        return now.replace(year=now.year - 1)
    except ValueError:  # 29 February
        return now.replace(year=now.year - 1, day=28)


async def clean(
    sessions: async_sessionmaker[AsyncSession], now: datetime, batch: int = BATCH
) -> Summary:
    started = time.perf_counter()
    summary = Summary()
    targets = (
        (OutboxEvent, OutboxEvent.published_at < now - timedelta(days=OUTBOX_DAYS)),
        (AuditLog, AuditLog.created_at < twelve_months_before(now)),
        (RefreshToken, RefreshToken.expires_at < now - timedelta(days=REFRESH_GRACE_DAYS)),
    )
    for model, expired in targets:
        async with sessions() as session:
            ids = select(model.id).where(expired).order_by(model.id).limit(batch)
            result = await session.execute(delete(model).where(model.id.in_(ids)))
            await session.commit()
        removed = int(getattr(result, "rowcount", 0) or 0)
        summary.processed += removed
        summary.changed += removed
    summary.duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info("retention %s", asdict(summary))
    return summary
