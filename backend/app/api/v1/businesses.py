import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import (
    CurrentUser,
    get_clock,
    get_current_user,
    get_session,
    require_business_member,
    require_owner_account,
    require_staff_or_owner_account,
)
from app.core.idempotency import IdempotentRoute, idempotency
from app.core.rate_limit import rate_limit, user_rate_limit
from app.core.security import AccessClaims
from app.models import Business, Service
from app.schemas.appointment import AvailabilityOut
from app.schemas.business import (
    BusinessCreate,
    BusinessOut,
    BusinessOwnerOut,
    BusinessPage,
    BusinessUpdate,
    MyBusinessOut,
    ServiceCreate,
    ServiceOut,
    ServiceUpdate,
    StaffCreate,
    StaffCreated,
    StaffOut,
    decode_cursor,
    encode_cursor,
)
from app.services import booking as booking_service
from app.services import business as business_service

router = APIRouter(prefix="/businesses", tags=["businesses"], route_class=IdempotentRoute)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ClockDep = Annotated[Callable[[], float], Depends(get_clock)]
# Dependency order is the error order: 401, then 404/403, then 429, then body validation.
OwnerAccount = Annotated[AccessClaims, Depends(require_owner_account)]
IDEMPOTENT = Depends(idempotency)
SERVICE_WRITE_LIMIT = Depends(user_rate_limit("service_write_user", limit=60, window_sec=3600))


@router.post(
    "",
    status_code=201,
    dependencies=[
        Depends(require_owner_account),
        Depends(user_rate_limit("business_create_user", limit=10, window_sec=3600)),
        Depends(idempotency),
    ],
)
async def create_business(
    body: BusinessCreate, user: OwnerAccount, session: SessionDep
) -> BusinessOut:
    business = await business_service.create_business(session, user.user_id, body)
    return BusinessOut.model_validate(business)


@router.get("", dependencies=[Depends(rate_limit("business_list_ip", limit=60, window_sec=60))])
async def list_businesses(
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=100)] = None,
    category: Annotated[str | None, Query(max_length=50)] = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> BusinessPage:
    after = decode_cursor(cursor) if cursor else None
    rows, has_more = await business_service.list_businesses(session, q, category, after, limit)
    last = rows[-1] if has_more else None
    return BusinessPage(
        items=[BusinessOut.model_validate(row) for row in rows],
        next_cursor=encode_cursor(last.name, last.id) if last else None,
    )


@router.get("/mine")
async def my_businesses(
    user: Annotated[AccessClaims, Depends(require_staff_or_owner_account)], session: SessionDep
) -> list[MyBusinessOut]:
    rows = await business_service.list_my_businesses(session, user.user_id)
    return [
        MyBusinessOut(
            id=b.id,
            name=b.name,
            category=b.category,
            address=b.address,
            timezone=b.timezone,
            open_hours=b.open_hours,
            role=role,
        )
        for b, role in rows
    ]


@router.get("/{business_id}")
async def get_business(business_id: uuid.UUID, session: SessionDep) -> BusinessOut:
    return BusinessOut.model_validate(await business_service.get_business(session, business_id))


@router.get("/{business_id}/services")
async def list_services(
    business_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    include_inactive: bool = False,
) -> list[ServiceOut]:
    if include_inactive:  # owner-only view: 401, then 404, then 403
        user = get_current_user(request)
        await require_business_member("owner")(business_id, user, session)
    services = await business_service.list_services(session, business_id, include_inactive)
    return [ServiceOut.model_validate(service) for service in services]


@router.post(
    "/{business_id}/services",
    status_code=201,
    dependencies=[Depends(require_business_member("owner")), SERVICE_WRITE_LIMIT, IDEMPOTENT],
)
async def create_service(
    business_id: uuid.UUID, body: ServiceCreate, session: SessionDep
) -> ServiceOut:
    service = await business_service.create_service(session, business_id, body)
    return ServiceOut.model_validate(service)


@router.patch(
    "/{business_id}/services/{service_id}",
    dependencies=[Depends(require_business_member("owner")), SERVICE_WRITE_LIMIT, IDEMPOTENT],
)
async def update_service(
    business_id: uuid.UUID, service_id: uuid.UUID, body: ServiceUpdate, session: SessionDep
) -> ServiceOut:
    service = await business_service.update_service(session, business_id, service_id, body)
    return ServiceOut.model_validate(service)


OwnerOnly = Depends(require_business_member("owner"))
STAFF_CREATE_LIMIT = Depends(user_rate_limit("staff_create_user", limit=20, window_sec=3600))
STAFF_DELETE_LIMIT = Depends(user_rate_limit("staff_delete_user", limit=60, window_sec=3600))


@router.patch("/{business_id}", dependencies=[OwnerOnly, IDEMPOTENT])
async def update_business(
    business_id: uuid.UUID, body: BusinessUpdate, session: SessionDep, clock: ClockDep
) -> BusinessOwnerOut:
    business = await business_service.update_business(session, business_id, body, clock)
    return BusinessOwnerOut.model_validate(business)


@router.get("/{business_id}/settings", dependencies=[OwnerOnly])
async def get_settings(business_id: uuid.UUID, session: SessionDep) -> BusinessOwnerOut:
    business = await business_service.get_business(session, business_id)
    return BusinessOwnerOut.model_validate(business)


@router.get("/{business_id}/staff", dependencies=[OwnerOnly])
async def list_staff(business_id: uuid.UUID, session: SessionDep) -> list[StaffOut]:
    staff = await business_service.list_staff(session, business_id)
    return [StaffOut.model_validate(user) for user in staff]


@router.post(
    "/{business_id}/staff",
    status_code=201,
    dependencies=[
        OwnerOnly,
        STAFF_CREATE_LIMIT,
        IDEMPOTENT,
    ],
)
async def create_staff(
    business_id: uuid.UUID, body: StaffCreate, session: SessionDep
) -> StaffCreated:
    user = await business_service.create_staff(session, business_id, body)
    return StaffCreated.model_validate(user)


@router.delete(
    "/{business_id}/staff/{user_id}",
    status_code=204,
    dependencies=[
        OwnerOnly,
        STAFF_DELETE_LIMIT,
    ],
)
async def remove_staff(business_id: uuid.UUID, user_id: uuid.UUID, session: SessionDep) -> None:
    await business_service.remove_staff(session, business_id, user_id)


@dataclass(frozen=True)
class AvailabilityTarget:
    business: Business
    service: Service


async def availability_target(
    business_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    service_id: Annotated[str | None, Query()] = None,
) -> AvailabilityTarget:
    """401, then business 404, then service 404; runs before the rate limit and date checks."""
    business = await business_service.get_business(session, business_id)
    service = await booking_service.find_service(session, business_id, service_id)
    return AvailabilityTarget(business, service)


@router.get(
    "/{business_id}/availability",
    dependencies=[
        Depends(availability_target),
        Depends(user_rate_limit("availability_user", limit=120, window_sec=60)),
    ],
)
async def availability(
    target: Annotated[AvailabilityTarget, Depends(availability_target)],
    session: SessionDep,
    clock: ClockDep,
    date: Annotated[str | None, Query()] = None,  # parsed in the service so 404s come first
) -> AvailabilityOut:
    return await booking_service.get_availability(
        session, target.business, target.service, date, clock
    )
