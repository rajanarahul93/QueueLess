"""The running-late job (FR-25) on real Postgres: on-time appointment check-ins only, one event
per change of state, read back from the outbox. The service is 30 minutes (1800 s) and the
slot is Monday 2 November 09:00 in Kolkata; walk-ins queued before it are served first."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import OutboxEvent
from app.services import queue_jobs
from tests.integration.api import Api
from tests.integration.test_booking import World, book_one, customer, world
from tests.integration.test_queues import queue_id
from tests.integration.test_serving import call_next
from tests.integration.test_tokens import check_in, join, leave

pytestmark = pytest.mark.anyio


def at(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-11-02T{clock}+05:30")


async def late_events(api: Api, token: str) -> list[tuple[str, int]]:
    async with api.sessions() as session:
        rows = (
            await session.execute(
                select(OutboxEvent.event_type, OutboxEvent.payload)
                .where(
                    OutboxEvent.event_type.in_(
                        (queue_jobs.RUNNING_LATE, queue_jobs.RUNNING_LATE_CLEARED)
                    ),
                    OutboxEvent.payload["token_id"].astext == token,
                )
                .order_by(OutboxEvent.id)
            )
        ).all()
    return [(kind, payload["delay_min"]) for kind, payload in rows]


async def queue_with(
    api: Api, walk_ins: int, now: datetime
) -> tuple[World, str, list[tuple[dict[str, str], str]]]:
    """Walk-ins joined at `now`, then the appointment checked in (on time) at `now`."""
    w = await world(api)
    _, headers = await customer(api)
    appointment_id = await book_one(w, headers)  # 09:00
    api.clock.now = now.timestamp()
    queue = await queue_id(w)
    walkers = []
    for _ in range(walk_ins):
        _, walker = await customer(api)
        walkers.append((walker, (await join(api, walker, queue)).json()["id"]))
    body = (await check_in(api, headers, appointment_id)).json()
    assert body["source"] == "appointment", body
    return w, body["id"], walkers


async def test_the_threshold_itself_is_late_and_one_minute_less_is_not(api: Api) -> None:
    # one 1800 s walk-in ahead: projected start = now + 30 min; at 08:40 that is 09:10, ten
    # minutes past the slot (the threshold); at 08:39 it is nine
    _, early, _ = await queue_with(api, 1, at("08:39:00"))
    await queue_jobs.running_late(api.sessions, at("08:39:00"))
    assert await late_events(api, early) == []
    _, token, _ = await queue_with(api, 1, at("08:40:00"))
    await queue_jobs.running_late(api.sessions, at("08:40:00"))
    assert await late_events(api, token) == [(queue_jobs.RUNNING_LATE, 10)]


async def test_one_event_per_change_then_cleared_when_the_delay_falls(api: Api) -> None:
    # two walk-ins ahead: 3600 s; at 08:45 the projected start is 09:45, 45 minutes late
    _, token, walkers = await queue_with(api, 2, at("08:45:00"))
    for minute in range(3):  # no flood: still late, nothing new
        await queue_jobs.running_late(api.sessions, at("08:45:00") + timedelta(minutes=minute))
    assert await late_events(api, token) == [(queue_jobs.RUNNING_LATE, 45)]
    for walker, walker_token in walkers:
        assert (await leave(api, walker, walker_token)).status_code == 204
    # nothing ahead: projected start 08:48, 12 minutes early: below 10 - 5, so cleared
    await queue_jobs.running_late(api.sessions, at("08:48:00"))
    await queue_jobs.running_late(api.sessions, at("08:49:00"))
    assert await late_events(api, token) == [
        (queue_jobs.RUNNING_LATE, 45),
        (queue_jobs.RUNNING_LATE_CLEARED, -12),
    ]


async def test_late_arrivals_and_called_tokens_get_nothing(api: Api) -> None:
    # checked in at 09:10 (after the 5-minute grace): queued at check-in, not at the slot
    w, late_arrival, _ = await queue_with(api, 2, at("09:10:00"))
    await queue_jobs.running_late(api.sessions, at("09:10:00"))
    assert await late_events(api, late_arrival) == []
    # an on-time appointment that is called no longer waits: no ETA, no event
    w, token, _ = await queue_with(api, 0, at("08:50:00"))
    queue = await queue_id(w)
    assert (await call_next(api, w.owner, queue)).json()["id"] == token
    await queue_jobs.running_late(api.sessions, at("09:30:00"))
    assert await late_events(api, token) == []


async def test_the_event_carries_the_new_range_and_no_personal_data(api: Api) -> None:
    _, token, _ = await queue_with(api, 2, at("08:45:00"))
    await queue_jobs.running_late(api.sessions, at("08:45:00"))
    async with api.sessions() as session:
        payload = await session.scalar(
            select(OutboxEvent.payload).where(
                OutboxEvent.event_type == queue_jobs.RUNNING_LATE,
                OutboxEvent.payload["token_id"].astext == token,
            )
        )
    assert payload is not None
    # 3600 -/+ 509.1 s -> 51.5 to 68.5 min -> 50 to 70
    assert payload["eta"] == {"min_minutes": 50, "max_minutes": 70}
    assert uuid.UUID(payload["token_id"]) and not {"name", "phone", "email", "customer_id"} & set(
        payload
    )
