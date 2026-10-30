import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.dependencies import CurrentUser, SessionDep, require_staff_or_owner_account
from app.core.rate_limit import user_rate_limit
from app.schemas.queue import SnapshotOut
from app.services import queue as queue_service

router = APIRouter(prefix="/queues", tags=["queues"])


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
