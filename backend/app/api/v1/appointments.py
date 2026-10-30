import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime

from app.core.config import Settings
from app.core.dependencies import (
    CurrentUser,
    SessionDep,
    account_role,
    assert_member,
    get_clock,
    get_settings,
    require_appointment_account,
    require_customer_account,
)
from app.core.errors import Forbidden, ValidationFailed
from app.core.idempotency import IdempotentRoute, idempotency
from app.core.rate_limit import RateLimiter, get_rate_limiter, user_rate_limit
from app.schemas.appointment import (
    AppointmentCreate,
    AppointmentOut,
    AppointmentPage,
    StaffAppointmentPage,
)
from app.services import appointments as appointment_service
from app.services import booking as booking_service
from app.services.appointments import Status

router = APIRouter(prefix="/appointments", tags=["appointments"], route_class=IdempotentRoute)

ClockDep = Annotated[Callable[[], float], Depends(get_clock)]
LIST_LIMIT = Depends(user_rate_limit("appointments_list_user", limit=120, window_sec=60))


async def booking_target(
    body: AppointmentCreate, session: SessionDep
) -> booking_service.BookingTarget:
    """404 for the service and resource; listed after the role check and before the rate limit."""
    return await booking_service.find_bookable(
        session, body.service_id, body.resource_id, body.start
    )


async def booking_rate_limit(
    user: CurrentUser,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    await limiter.check(
        "booking_user", str(user.user_id), settings.booking_rate_limit_per_hour, 3600
    )


BookingTarget = Annotated[booking_service.BookingTarget, Depends(booking_target)]


@router.post(
    "",
    status_code=201,
    dependencies=[
        Depends(require_customer_account),
        Depends(booking_target),
        Depends(booking_rate_limit),
        Depends(idempotency),
    ],
)
async def book(
    target: BookingTarget, user: CurrentUser, session: SessionDep, clock: ClockDep
) -> AppointmentOut:
    return await booking_service.book_appointment(session, user.user_id, target, clock)


@router.delete(
    "/{appointment_id}", status_code=204, dependencies=[Depends(require_appointment_account)]
)
async def cancel(
    appointment_id: uuid.UUID, user: CurrentUser, session: SessionDep, clock: ClockDep
) -> None:
    now = datetime.fromtimestamp(clock(), UTC)
    await appointment_service.cancel_appointment(session, user.user_id, appointment_id, now)


@dataclass(frozen=True)
class ListScope:
    customer_id: uuid.UUID | None = None
    business_id: uuid.UUID | None = None


async def list_scope(
    user: CurrentUser,
    session: SessionDep,
    business_id: Annotated[str | None, Query()] = None,  # raw, so the 401/403/404 order holds
) -> ListScope:
    """401, then 403 (admin), then business_id 422, 404, 403; customers may not pass it."""
    role = await account_role(session, user.user_id)
    if role == "admin":
        raise Forbidden("Not allowed for this account")
    if role == "customer":
        if business_id is not None:
            raise ValidationFailed(["query", "business_id"], "Not allowed for customers")
        return ListScope(customer_id=user.user_id)
    try:
        parsed = uuid.UUID(business_id or "")
    except ValueError:
        raise ValidationFailed(
            ["query", "business_id"], "A valid business_id is required"
        ) from None
    await assert_member(session, parsed, user.user_id, "owner", "staff")
    return ListScope(business_id=parsed)


@router.get("", dependencies=[Depends(list_scope), LIST_LIMIT])
async def list_appointments(
    scope: Annotated[ListScope, Depends(list_scope)],
    session: SessionDep,
    status: Annotated[Status | None, Query()] = None,
    start_from: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    start_to: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> AppointmentPage | StaffAppointmentPage:
    if start_from and start_to and start_from >= start_to:
        raise ValidationFailed(["query", "to"], "to must be after from")
    return await appointment_service.list_appointments(
        session,
        customer_id=scope.customer_id,
        business_id=scope.business_id,
        status=status,
        start_from=start_from,
        start_to=start_to,
        cursor=cursor,
        limit=limit,
    )
