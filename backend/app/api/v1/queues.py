import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.core.config import Settings
from app.core.dependencies import (
    CurrentUser,
    SessionDep,
    get_clock,
    get_settings,
    require_customer_account,
    require_staff_or_owner_account,
)
from app.core.errors import NotFound
from app.core.idempotency import IdempotentRoute, idempotency
from app.core.rate_limit import token_create_rate_limit, user_rate_limit
from app.models import Queue
from app.schemas.queue import (
    JoinIn,
    QueueOut,
    QueueStatusIn,
    SnapshotOut,
    SnapshotToken,
    StreamTokenOut,
    TokenOut,
)
from app.services import queue as queue_service

router = APIRouter(prefix="/queues", tags=["queues"], route_class=IdempotentRoute)

ClockDep = Annotated[Callable[[], float], Depends(get_clock)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def member_queue(
    queue_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> queue_service.MemberQueue:
    """404 for an unknown queue and for members of other businesses; before the rate limit."""
    return await queue_service.find_member_queue(session, queue_id, user.user_id)


MemberQueue = Annotated[queue_service.MemberQueue, Depends(member_queue)]


@router.get(
    "/{queue_id}",
    dependencies=[
        Depends(require_staff_or_owner_account),  # customers and admins: 403 before the id
        Depends(member_queue),
        Depends(user_rate_limit("queue_snapshot_user", limit=120, window_sec=60)),
    ],
)
async def snapshot(found: MemberQueue, session: SessionDep) -> SnapshotOut:
    return await queue_service.get_snapshot(session, found)


@router.patch(
    "/{queue_id}",
    dependencies=[
        Depends(require_staff_or_owner_account),
        Depends(member_queue),
        Depends(user_rate_limit("queue_status_user", limit=60, window_sec=60)),
        Depends(idempotency),
    ],
)
async def set_status(
    queue_id: uuid.UUID,
    body: QueueStatusIn,
    user: CurrentUser,
    session: SessionDep,
    clock: ClockDep,
) -> QueueOut:
    now = datetime.fromtimestamp(clock(), UTC)
    return await queue_service.set_queue_status(session, queue_id, user.user_id, body.status, now)


async def existing_queue(queue_id: uuid.UUID, session: SessionDep) -> None:
    """Queue ids are not public: an unknown one is 404, before the rate limit."""
    if await session.get(Queue, queue_id) is None:
        raise NotFound("Queue not found")


@router.post(
    "/{queue_id}/tokens",
    status_code=201,
    dependencies=[
        Depends(require_customer_account),
        Depends(existing_queue),
        Depends(token_create_rate_limit),
        Depends(idempotency),
    ],
)
async def join(
    queue_id: uuid.UUID, body: JoinIn, user: CurrentUser, session: SessionDep, clock: ClockDep
) -> TokenOut:
    now = datetime.fromtimestamp(clock(), UTC)
    return await queue_service.join_queue(session, queue_id, user.user_id, now)


@router.post(
    "/{queue_id}/call-next",
    response_model=SnapshotToken,
    responses={204: {"description": "Nobody is waiting"}},
    dependencies=[
        Depends(require_staff_or_owner_account),
        Depends(member_queue),
        Depends(user_rate_limit("call_next_user", limit=120, window_sec=60)),
        Depends(idempotency),  # a replayed press returns the same called token
    ],
)
async def call_next(
    queue_id: uuid.UUID, user: CurrentUser, session: SessionDep, clock: ClockDep
) -> SnapshotToken | Response:
    now = datetime.fromtimestamp(clock(), UTC)
    called = await queue_service.call_next(session, queue_id, user.user_id, now)
    return called if called is not None else Response(status_code=204)


@router.post(
    "/{queue_id}/stream-token",
    dependencies=[
        Depends(require_staff_or_owner_account),
        Depends(member_queue),  # membership is checked when the token is issued
        Depends(user_rate_limit("stream_token_user", limit=60, window_sec=60)),
    ],
)
async def stream_token(
    queue_id: uuid.UUID, user: CurrentUser, settings: SettingsDep
) -> StreamTokenOut:
    return queue_service.issue_stream_token(settings, queue_id, user.user_id)
