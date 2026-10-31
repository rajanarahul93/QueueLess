import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import CurrentUser, SessionDep, get_clock, require_customer_account
from app.core.errors import ValidationFailed
from app.core.rate_limit import user_rate_limit
from app.schemas.queue import ActiveTokenOut
from app.services import queue as queue_service

router = APIRouter(prefix="/tokens", tags=["tokens"])

ClockDep = Annotated[Callable[[], float], Depends(get_clock)]


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


@router.delete("/{token_id}", status_code=204, dependencies=[Depends(require_customer_account)])
async def leave(
    token_id: uuid.UUID, user: CurrentUser, session: SessionDep, clock: ClockDep
) -> None:
    """Staff cancellation is a later, separate staff action."""
    now = datetime.fromtimestamp(clock(), UTC)
    await queue_service.leave_queue(session, token_id, user.user_id, now)
