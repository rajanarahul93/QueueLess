import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.config import Settings
from app.core.dependencies import (
    CurrentUser,
    SessionDep,
    get_clock,
    get_settings,
    require_customer_account,
    require_staff_or_owner_account,
)
from app.core.errors import ValidationFailed
from app.core.idempotency import IdempotentRoute, idempotency
from app.core.rate_limit import user_rate_limit
from app.core.security import create_stream_token
from app.schemas.queue import (
    ActiveTokenOut,
    PriorityIn,
    SnapshotToken,
    StartIn,
    TokenWithStreamOut,
    VersionIn,
)
from app.services import queue as queue_service
from app.services.transitions import Event

router = APIRouter(prefix="/tokens", tags=["tokens"], route_class=IdempotentRoute)

ClockDep = Annotated[Callable[[], float], Depends(get_clock)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get(
    "",
    dependencies=[
        Depends(require_customer_account),
        Depends(user_rate_limit("active_tokens_user", limit=120, window_sec=60)),
    ],
)
async def active_tokens(
    user: CurrentUser,
    session: SessionDep,
    active: Annotated[str | None, Query()] = None,  # raw, so only the literal "true" passes
) -> list[ActiveTokenOut]:
    if active != "true":
        raise ValidationFailed(["query", "active"], "Only active=true is supported")
    return await queue_service.list_active_tokens(session, user.user_id)


@router.get("/{token_id}", dependencies=[Depends(require_customer_account)])  # staff: 403
async def get_token(
    token_id: uuid.UUID, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> TokenWithStreamOut:
    """The customer's own token plus a short-lived stream token for its live stream."""
    token = await queue_service.own_token(session, token_id, user.user_id)
    return TokenWithStreamOut(
        **token.model_dump(),
        stream_token=create_stream_token(settings, "token", token_id, user.user_id),
        expires_in=settings.stream_token_ttl_sec,
    )


@router.delete("/{token_id}", status_code=204, dependencies=[Depends(require_customer_account)])
async def leave(
    token_id: uuid.UUID, user: CurrentUser, session: SessionDep, clock: ClockDep
) -> None:
    """Staff cancellation is a later, separate staff action."""
    now = datetime.fromtimestamp(clock(), UTC)
    await queue_service.leave_queue(session, token_id, user.user_id, now)


async def member_token(token_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    """Owner or staff of the token's business; anyone else 404, before the rate limit."""
    await queue_service.find_member_token(session, token_id, user.user_id)


STAFF_ACTION = [
    Depends(require_staff_or_owner_account),  # customers and admins: 403 before the id
    Depends(member_token),
    Depends(user_rate_limit("token_action_user", limit=120, window_sec=60)),
    Depends(idempotency),
]


@router.post("/{token_id}/start", dependencies=STAFF_ACTION)
async def start(
    token_id: uuid.UUID, body: StartIn, user: CurrentUser, session: SessionDep, clock: ClockDep
) -> SnapshotToken:
    now = datetime.fromtimestamp(clock(), UTC)
    return await queue_service.act_on_token(
        session, token_id, user.user_id, "start", body.version, now, body.service_id
    )


async def _act(
    event: Event,
    token_id: uuid.UUID,
    body: VersionIn,
    user: CurrentUser,
    session: SessionDep,
    clock: ClockDep,
) -> SnapshotToken:
    now = datetime.fromtimestamp(clock(), UTC)
    return await queue_service.act_on_token(
        session, token_id, user.user_id, event, body.version, now
    )


@router.post("/{token_id}/complete", dependencies=STAFF_ACTION)
async def complete(
    token_id: uuid.UUID, body: VersionIn, user: CurrentUser, session: SessionDep, clock: ClockDep
) -> SnapshotToken:
    return await _act("complete", token_id, body, user, session, clock)


@router.post("/{token_id}/skip", dependencies=STAFF_ACTION)
async def skip(
    token_id: uuid.UUID, body: VersionIn, user: CurrentUser, session: SessionDep, clock: ClockDep
) -> SnapshotToken:
    return await _act("skip", token_id, body, user, session, clock)


@router.post("/{token_id}/no-show", dependencies=STAFF_ACTION)
async def no_show(
    token_id: uuid.UUID, body: VersionIn, user: CurrentUser, session: SessionDep, clock: ClockDep
) -> SnapshotToken:
    return await _act("mark_no_show", token_id, body, user, session, clock)


@router.patch("/{token_id}/priority", dependencies=STAFF_ACTION)
async def priority(
    token_id: uuid.UUID, body: PriorityIn, user: CurrentUser, session: SessionDep, clock: ClockDep
) -> SnapshotToken:
    now = datetime.fromtimestamp(clock(), UTC)
    return await queue_service.set_priority(
        session, token_id, user.user_id, body.priority, body.version, now
    )
