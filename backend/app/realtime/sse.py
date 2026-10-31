"""Server-sent event streams: staff queue, customer token, public board (FR-20 to FR-24, FR-35).

Every stream: join the queue's channel (subscription confirmed), then build the first frames in
one REPEATABLE READ transaction (a snapshot plus the queue's highest outbox id, or a replay after
Last-Event-ID), then pass live events through, dropping any id already covered. Ids are the
outbox ids; a queue's ids commit in order, so `id <= last sent` is a duplicate.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import NotFound
from app.models import OutboxEvent, QueueToken, User
from app.realtime.hub import RESYNC, Client, Hub
from app.realtime.relay import envelope
from app.services import queue as queue_service

HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
RETRY_MS = 3000
RECHECK_SEC = 60.0
MEDIA_TYPE = "text/event-stream"

Event = dict[str, Any]
Frames = list[str]


def frame(kind: str, data: Any, event_id: int | None = None) -> str:  # Any: JSON-ready data
    lines = [f"id: {event_id}"] if event_id is not None else []
    lines += [f"event: {kind}", "data: " + json.dumps(data, default=str)]
    return "\n".join(lines) + "\n\n"


@dataclass
class View:
    """How one kind of stream turns the database and the queue's events into frames."""

    queue_id: uuid.UUID
    snapshot: Callable[[AsyncSession], Awaitable[Any]]  # Any: the view's JSON snapshot
    on_event: Callable[[Event], Awaitable[Frames]]
    catch_up: Callable[[list[Event]], Awaitable[Frames]]
    recheck: Callable[[], Awaitable[bool]] | None = None


async def first_frames(
    sessions: async_sessionmaker[AsyncSession],
    hub: Hub,
    client: Client,
    view: View,
    last: str | None,
) -> Frames:
    """A replay after a known Last-Event-ID, otherwise (with a reset when an id was given) a
    snapshot carrying the queue's highest outbox id. One snapshot, so the two agree."""
    async with hub.snapshots, sessions() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        frames: Frames = []
        resume = int(last) if last is not None and last.isdigit() else None
        if resume is not None:
            known = await session.scalar(
                select(OutboxEvent.id).where(
                    OutboxEvent.id == resume, OutboxEvent.queue_id == view.queue_id
                )
            )
            if known is not None:
                rows = (
                    await session.scalars(
                        select(OutboxEvent)
                        .where(OutboxEvent.queue_id == view.queue_id, OutboxEvent.id > resume)
                        .order_by(OutboxEvent.id)
                    )
                ).all()
                client.last_id = rows[-1].id if rows else resume
                return await view.catch_up([envelope(row) for row in rows])
        if last is not None:
            oldest = await session.scalar(
                select(func.min(OutboxEvent.id)).where(OutboxEvent.queue_id == view.queue_id)
            )
            expired = resume is not None and (oldest is None or resume < oldest)
            frames.append(frame("reset", {"reason": "expired" if expired else "unknown"}))
        data = await view.snapshot(session)
        highest = await session.scalar(
            select(func.max(OutboxEvent.id)).where(OutboxEvent.queue_id == view.queue_id)
        )
        client.last_id = highest or 0
        frames.append(frame("snapshot", data, highest))
        return frames


async def stream(
    hub: Hub,
    client: Client,
    sessions: async_sessionmaker[AsyncSession],
    view: View,
    frames: Frames,
    heartbeat_sec: float,
    lifetime_sec: float,
) -> AsyncIterator[str]:
    """The body of every stream. Ends at the lifetime cap, when the client falls behind, when
    a staff member loses membership, or when the client disconnects; always releases."""
    try:
        yield f"retry: {RETRY_MS}\n\n"
        for chunk in frames:
            yield chunk
        deadline = time.monotonic() + lifetime_sec
        next_check = time.monotonic() + RECHECK_SEC
        while True:
            now = time.monotonic()
            if now >= deadline:
                return
            wait = min(heartbeat_sec, deadline - now)
            if view.recheck is not None:
                wait = min(wait, max(next_check - now, 0))
            try:
                item = await asyncio.wait_for(client.events.get(), wait)
            except TimeoutError:
                item = None
            if client.overflow:
                return  # a slow consumer, or the instance is shutting down
            if view.recheck is not None and time.monotonic() >= next_check:
                if not await view.recheck():
                    yield frame("revoked", {})
                    return
                next_check = time.monotonic() + RECHECK_SEC
            if item is None:
                yield ": heartbeat\n\n"
            elif item == RESYNC:
                for chunk in await _resync(sessions, client, view):
                    yield chunk
            elif isinstance(item, dict) and item["id"] > client.last_id:
                client.last_id = item["id"]
                for chunk in await view.on_event(item):
                    yield chunk
    finally:
        await hub.release(client)


async def _resync(sessions: async_sessionmaker[AsyncSession], client: Client, view: View) -> Frames:
    """After the Pub/Sub connection dropped: everything after the last id sent, from the DB."""
    async with sessions() as session:
        rows = (
            await session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.queue_id == view.queue_id, OutboxEvent.id > client.last_id)
                .order_by(OutboxEvent.id)
            )
        ).all()
    if rows:
        client.last_id = rows[-1].id
    return await view.catch_up([envelope(row) for row in rows])


# --- the three views ---


def staff_view(
    sessions: async_sessionmaker[AsyncSession], hub: Hub, queue_id: uuid.UUID, user_id: uuid.UUID
) -> View:
    async def snapshot(session: AsyncSession) -> Any:  # Any: JSON
        found = await queue_service.find_member_queue(session, queue_id, user_id)
        return (await queue_service.get_snapshot(session, found)).model_dump(mode="json")

    async def customer_of(token_id: str) -> dict[str, str] | None:
        async with sessions() as session:
            row = (
                await session.execute(
                    select(User.id, User.name)
                    .join(QueueToken, QueueToken.customer_id == User.id)
                    .where(QueueToken.id == uuid.UUID(token_id))
                )
            ).one_or_none()
        return {"id": str(row.id), "name": row.name} if row else None

    async def on_event(event: Event) -> Frames:
        data = dict(event["data"])
        if "token_id" in data:  # staff see the customer's id and name, nothing more
            data["customer"] = await hub.cached(
                ("staff", event["id"]), lambda: customer_of(data["token_id"])
            )
        return [frame(event["type"], {**event, "data": data}, event["id"])]

    async def catch_up(events: list[Event]) -> Frames:
        return [chunk for event in events for chunk in await on_event(event)]

    async def recheck() -> bool:
        async with sessions() as session:
            try:
                await queue_service.find_member_queue(session, queue_id, user_id)
            except NotFound:
                return False
        return True

    return View(queue_id, snapshot, on_event, catch_up, recheck)


def customer_view(
    sessions: async_sessionmaker[AsyncSession], hub: Hub, queue_id: uuid.UUID, token_id: uuid.UUID
) -> View:
    """Only this customer's own token: status, number, waiting tokens ahead, and whether it is
    their turn. Sent only when that changes (FR-24); never another token, never priority."""
    sent: dict[str, Any] = {}

    async def current(session: AsyncSession) -> dict[str, Any]:
        token = await session.get(QueueToken, token_id)
        assert token is not None
        ahead = await session.scalar(
            select(func.count()).where(
                QueueToken.queue_id == queue_id,
                QueueToken.status == "waiting",
                queue_service.ahead_of(QueueToken, token),
            )
        )
        return _customer_state(token.id, token.token_no, token.status, ahead or 0)

    async def queue_states() -> dict[str, tuple[int, str, int | None]]:
        async with sessions() as session:
            rows = (await session.execute(queue_service.active_tokens_with_ahead(queue_id))).all()
        return {str(row.id): (row.token_no, row.status, row.ahead) for row in rows}

    async def snapshot(session: AsyncSession) -> Any:  # Any: JSON
        state = await current(session)
        sent.update(state)
        return state

    async def changed(state: dict[str, Any], event_id: int) -> Frames:
        if state == sent:
            return []
        sent.clear()
        sent.update(state)
        return [frame("token.updated", state, event_id)]

    async def on_event(event: Event) -> Frames:
        states = await hub.cached(("customer", event["id"]), queue_states)
        mine = str(token_id)
        if mine in states:
            number, status, ahead = states[mine]
            state = _customer_state(token_id, number, status, ahead or 0)
        elif event["data"].get("token_id") == mine:  # it just left the active tokens
            state = _customer_state(token_id, event["data"]["token_no"], event["data"]["status"], 0)
        else:
            return []
        return await changed(state, event["id"])

    async def catch_up(events: list[Event]) -> Frames:
        if not events:
            return []
        async with sessions() as session:
            state = await current(session)
        return await changed(state, events[-1]["id"])

    return View(queue_id, snapshot, on_event, catch_up)


def _customer_state(token_id: uuid.UUID, number: int, status: str, ahead: int) -> dict[str, Any]:
    waiting = status == "waiting"
    return {
        "token_id": str(token_id),
        "token_no": number,
        "status": status,
        "ahead_count": ahead if waiting else None,
        "next": status == "called" or (waiting and ahead == 0),
    }


def board_view(sessions: async_sessionmaker[AsyncSession], hub: Hub, queue_id: uuid.UUID) -> View:
    """Token numbers only (FR-35): now serving, and the next five in call order."""
    sent: dict[str, Any] = {}

    async def board(session: AsyncSession) -> dict[str, Any]:
        return await queue_service.board(session, queue_id)

    async def fresh() -> dict[str, Any]:
        async with sessions() as session:
            return await board(session)

    async def snapshot(session: AsyncSession) -> Any:  # Any: JSON
        sent.update(await board(session))
        return dict(sent)

    async def on_event(event: Event) -> Frames:
        state = await hub.cached(("board", event["id"]), fresh)
        if state == sent:
            return []
        sent.clear()
        sent.update(state)
        return [frame("board.updated", state, event["id"])]

    async def catch_up(events: list[Event]) -> Frames:
        return await on_event(events[-1]) if events else []

    return View(queue_id, snapshot, on_event, catch_up)


class RedactStreamToken(logging.Filter):
    """Stream tokens travel in the query string; keep them out of the access log."""

    PATTERN = re.compile(r"(stream_token=)[^&\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self.PATTERN.sub(r"\1[redacted]", arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        record.msg = self.PATTERN.sub(r"\1[redacted]", str(record.msg))
        return True
