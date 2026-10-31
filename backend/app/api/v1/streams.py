import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.background import BackgroundTask

from app.core.config import Settings
from app.core.dependencies import SessionDep, get_clock, get_settings
from app.core.errors import NotFound, Unauthenticated
from app.core.security import StreamClaims, StreamScope, verify_stream_token
from app.core.timezones import get_zone
from app.models import QueueToken
from app.realtime.hub import Client, Hub
from app.realtime.sse import (
    HEADERS,
    MEDIA_TYPE,
    View,
    board_view,
    customer_view,
    first_frames,
    staff_view,
    stream,
)
from app.services import queue as queue_service
from app.services.business import get_business

router = APIRouter(tags=["streams"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
ClockDep = Annotated[Callable[[], float], Depends(get_clock)]
StreamToken = Annotated[str | None, Query()]  # EventSource cannot send an Authorization header
LastEventId = Annotated[str | None, Header(alias="Last-Event-ID")]


def _claims_for(
    settings: Settings, token: str | None, scope: StreamScope, resource: uuid.UUID
) -> StreamClaims:
    if not token:
        raise Unauthenticated("A stream_token is required")
    return verify_stream_token(settings, token, scope, resource)


async def _open(
    request: Request,
    client: Client,
    view: View,
    last_event_id: str | None,
    lifetime_sec: float,
) -> StreamingResponse:
    """Subscribe, build the first frames, then hand the stream over. Any failure before the
    response starts releases the connection slot and surfaces as a normal error response."""
    hub: Hub = request.app.state.hub
    sessions: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    settings: Settings = request.app.state.settings
    try:
        await hub.join(client, f"queue:{view.queue_id}")
        frames = await first_frames(sessions, hub, client, view, last_event_id)
    except BaseException:
        await hub.release(client)
        raise
    body = stream(hub, client, sessions, view, frames, settings.sse_heartbeat_sec, lifetime_sec)
    return StreamingResponse(
        body,
        media_type=MEDIA_TYPE,
        headers=HEADERS,
        background=BackgroundTask(hub.release, client),  # also when the body never started
    )


@router.get("/queues/{queue_id}/stream")
async def staff_stream(
    queue_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    stream_token: StreamToken = None,
    last_event_id: LastEventId = None,
) -> StreamingResponse:
    """Owner or staff of the queue's business, checked now and every minute after."""
    claims = _claims_for(settings, stream_token, "queue", queue_id)
    await queue_service.find_member_queue(session, queue_id, claims.subject_id)  # 404 otherwise
    await session.close()
    hub: Hub = request.app.state.hub
    client = hub.admit(str(claims.subject_id))
    sessions = request.app.state.session_factory
    view = staff_view(sessions, hub, queue_id, claims.subject_id)
    return await _open(request, client, view, last_event_id, settings.sse_max_stream_sec)


@router.get("/tokens/{token_id}/stream")
async def customer_stream(
    token_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    stream_token: StreamToken = None,
    last_event_id: LastEventId = None,
) -> StreamingResponse:
    """The token's own customer only; their status, number, tokens ahead and turn."""
    claims = _claims_for(settings, stream_token, "token", token_id)
    token = await session.get(QueueToken, token_id)
    if token is None or token.customer_id != claims.subject_id:
        raise NotFound("Token not found")
    queue_id = token.queue_id
    await session.close()
    hub: Hub = request.app.state.hub
    client = hub.admit(str(claims.subject_id))
    view = customer_view(
        request.app.state.session_factory,
        hub,
        queue_id,
        token_id,
        request.app.state.redis,
        request.app.state.clock,
        settings.eta_cache_ttl_sec,
    )
    return await _open(request, client, view, last_event_id, settings.sse_max_stream_sec)


@router.get("/businesses/{business_id}/board")
async def board_stream(
    business_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
    last_event_id: LastEventId = None,
) -> StreamingResponse:
    """Public, token numbers only. Today's queue (created on first access, as the lookup does);
    the stream ends at local midnight so the screen reconnects to the new day's queue."""
    now = datetime.fromtimestamp(clock(), UTC)
    business = await get_business(session, business_id)
    queue = await queue_service.get_or_create_today_queue(session, business, now)
    await session.commit()
    zone = get_zone(business.timezone)
    local = now.astimezone(zone)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), zone)
    until_midnight = (midnight.astimezone(UTC) - now).total_seconds()
    hub: Hub = request.app.state.hub
    client = hub.admit(None, board=True)
    view = board_view(request.app.state.session_factory, hub, queue.id)
    lifetime = max(1.0, min(settings.sse_max_stream_sec, until_midnight))
    return await _open(request, client, view, last_event_id, lifetime)
