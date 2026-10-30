import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, tuple_, type_coerce, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, ValidationFailed
from app.models import Appointment, Business, BusinessMember, Resource, Service, User
from app.models.appointment import ACTIVE_STATUSES
from app.schemas.business import (
    BusinessCreate,
    BusinessUpdate,
    ServiceCreate,
    ServiceUpdate,
    StaffCreate,
)
from app.services.auth import create_password_user

DEFAULT_RESOURCE_NAME = "Main"


def default_settings() -> dict[str, Any]:  # Any: jsonb values are mixed ints and objects
    """PRD D6 defaults; max_walk_ins is walk-in tokens allowed per queue per day."""
    return {
        "grace_period_min": 5,
        "reschedule_cutoff_min": 120,
        "promotion_wait_limit_min": 20,
        "walk_in_ratio": {"walk_ins": 1, "appointments": 2},
        "running_late_threshold_min": 10,
        "max_walk_ins": 50,
    }


async def create_business(
    session: AsyncSession, owner_id: uuid.UUID, data: BusinessCreate
) -> Business:
    """Business, default resource and owner membership commit together or not at all."""
    business = Business(owner_id=owner_id, settings=default_settings(), **data.model_dump())
    session.add(business)
    await session.flush()
    session.add(Resource(business_id=business.id, name=DEFAULT_RESOURCE_NAME))
    session.add(BusinessMember(business_id=business.id, user_id=owner_id, role="owner"))
    await session.commit()
    return business


async def update_business(
    session: AsyncSession,
    business_id: uuid.UUID,
    data: BusinessUpdate,
    clock: Callable[[], float],
) -> Business:
    """One UPDATE: `settings || patch` is merged by Postgres under the row lock, so parallel
    PATCHes of different keys both survive."""
    values = data.model_dump(exclude_unset=True, exclude={"settings"})
    if "timezone" in values:
        await _check_timezone_change(session, business_id, values["timezone"], clock)
    if data.settings is not None:
        patch = data.settings.model_dump(exclude_unset=True)
        values["settings"] = Business.settings.op("||")(type_coerce(patch, JSONB))
    business = (
        await session.execute(
            update(Business).where(Business.id == business_id).values(**values).returning(Business)
        )
    ).scalar_one()
    await session.commit()
    return business


async def _check_timezone_change(
    session: AsyncSession, business_id: uuid.UUID, new_zone: str, clock: Callable[[], float]
) -> None:
    """Stored appointment instants stay valid, but the local grid and display would shift under
    a live appointment, so a change is refused while one is active and not yet over.
    A booking racing this check can still slip through (accepted)."""
    if new_zone == await session.scalar(
        select(Business.timezone).where(Business.id == business_id)
    ):
        return
    now = datetime.fromtimestamp(clock(), UTC)
    pending = await session.scalar(
        select(Appointment.id)
        .where(
            Appointment.business_id == business_id,
            Appointment.status.in_(ACTIVE_STATUSES),
            func.upper(Appointment.during) > now,
        )
        .limit(1)
    )
    if pending is not None:
        raise ValidationFailed(
            ["body", "timezone"], "Cannot change the time zone while appointments are active"
        )


async def list_my_businesses(
    session: AsyncSession, user_id: uuid.UUID
) -> list[tuple[Business, str]]:
    rows = await session.execute(
        select(Business, BusinessMember.role)
        .join(BusinessMember, BusinessMember.business_id == Business.id)
        .where(BusinessMember.user_id == user_id)
        .order_by(Business.name, Business.id)
    )
    return [(business, role) for business, role in rows.all()]


async def create_staff(session: AsyncSession, business_id: uuid.UUID, data: StaffCreate) -> User:
    """User and membership commit together; a taken email is a 422 and attaches nothing."""
    user = await create_password_user(
        session, data.name, data.email, data.initial_password, "staff"
    )
    session.add(BusinessMember(business_id=business_id, user_id=user.id, role="staff"))
    await session.commit()
    return user


async def list_staff(session: AsyncSession, business_id: uuid.UUID) -> list[User]:
    query = (
        select(User)
        .join(BusinessMember, BusinessMember.user_id == User.id)
        .where(BusinessMember.business_id == business_id, BusinessMember.role == "staff")
        .order_by(User.name, User.id)
    )
    return list((await session.scalars(query)).all())


async def remove_staff(session: AsyncSession, business_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Only a staff membership of THIS business; the account stays. Anything else is a 404."""
    removed = await session.scalar(
        delete(BusinessMember)
        .where(
            BusinessMember.business_id == business_id,
            BusinessMember.user_id == user_id,
            BusinessMember.role == "staff",
        )
        .returning(BusinessMember.user_id)
    )
    if removed is None:
        raise NotFound("Staff member not found")
    await session.commit()


async def list_businesses(
    session: AsyncSession,
    q: str | None,
    category: str | None,
    after: tuple[str, uuid.UUID] | None,
    limit: int,
) -> tuple[list[Business], bool]:
    """Keyset page ordered by (name, id); returns the rows and whether more follow."""
    query = select(Business).order_by(Business.name, Business.id).limit(limit + 1)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(Business.name.ilike(f"%{escaped}%", escape="\\"))
    if category:
        query = query.where(Business.category == category)
    if after:
        query = query.where(tuple_(Business.name, Business.id) > tuple_(*after))
    rows = list((await session.scalars(query)).all())
    return rows[:limit], len(rows) > limit


async def get_business(session: AsyncSession, business_id: uuid.UUID) -> Business:
    business = await session.get(Business, business_id)
    if business is None:
        raise NotFound("Business not found")
    return business


async def list_services(
    session: AsyncSession, business_id: uuid.UUID, include_inactive: bool
) -> list[Service]:
    await get_business(session, business_id)
    query = (
        select(Service).where(Service.business_id == business_id).order_by(Service.name, Service.id)
    )
    if not include_inactive:
        query = query.where(Service.is_active)
    return list((await session.scalars(query)).all())


async def create_service(
    session: AsyncSession, business_id: uuid.UUID, data: ServiceCreate
) -> Service:
    service = Service(business_id=business_id, **data.model_dump())
    session.add(service)
    await session.commit()
    await session.refresh(service)  # price comes back in the column's numeric(10,2) form
    return service


async def update_service(
    session: AsyncSession, business_id: uuid.UUID, service_id: uuid.UUID, data: ServiceUpdate
) -> Service:
    # Both ids must match, so a service of another business is a 404 even for a valid owner.
    service = await session.scalar(
        select(Service).where(Service.id == service_id, Service.business_id == business_id)
    )
    if service is None:
        raise NotFound("Service not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(service, field, value)
    await session.commit()
    await session.refresh(service)
    return service
