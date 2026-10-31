import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
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
from app.core.rate_limit import (
    RateLimiter,
    get_rate_limiter,
    token_create_rate_limit,
    user_rate_limit,
)
from app.models import Appointment
from app.schemas.appointment import (
    AppointmentCreate,
    AppointmentOut,
    AppointmentPage,
    RescheduledOut,
    RescheduleIn,
    StaffAppointmentPage,
)
from app.schemas.queue import TokenOut
from app.services import appointments as appointment_service
from app.services import booking as booking_service
from app.services import queue as queue_service
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


async def own_appointment(
    appointment_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Appointment:
    """404 for another customer's appointment; runs before the rate limit."""
    return await appointment_service.find_own(session, appointment_id, user.user_id)


OwnAppointment = Annotated[Appointment, Depends(own_appointment)]


@router.patch(
    "/{appointment_id}/reschedule",
    dependencies=[
        Depends(require_customer_account),
        Depends(own_appointment),
        Depends(booking_rate_limit),  # shares the booking bucket
        Depends(idempotency),
    ],
)
async def reschedule(
    old: OwnAppointment,
    body: RescheduleIn,
    user: CurrentUser,
    session: SessionDep,
    clock: ClockDep,
) -> RescheduledOut:
    target = await booking_service.find_bookable(
        session, old.service_id, old.resource_id, body.start
    )
    new = await booking_service.book_appointment(
        session, user.user_id, target, clock, replaces=old.id
    )
    return RescheduledOut(**new.model_dump(), rescheduled_from_id=old.id)


async def check_in_target(
    appointment_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> None:
    """The owning customer, or owner/staff of its business; anyone else 404, before the limit."""
    await queue_service.visible_appointment(session, appointment_id, user.user_id)


@router.post(
    "/{appointment_id}/check-in",
    status_code=201,
    dependencies=[
        Depends(require_appointment_account),  # admins: 403
        Depends(check_in_target),
        Depends(token_create_rate_limit),
        Depends(idempotency),
    ],
)
async def check_in(
    appointment_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    clock: ClockDep,
    response: Response,
) -> TokenOut:
    """201 with a new token; a repeated check-in returns the same token with 200."""
    now = datetime.fromtimestamp(clock(), UTC)
    token, created = await queue_service.check_in(session, appointment_id, user.user_id, now)
    if not created:
        response.status_code = 200
    return token


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
