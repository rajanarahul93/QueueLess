import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import ColumnElement, Row, exists, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidTransition, NotFound, ValidationFailed
from app.core.timezones import get_zone
from app.models import Appointment, Business, BusinessMember, Service, User
from app.realtime.outbox import emit_event
from app.schemas.appointment import (
    AppointmentOut,
    AppointmentPage,
    CustomerOut,
    StaffAppointmentOut,
    StaffAppointmentPage,
)
from app.schemas.business import decode_cursor, encode_cursor
from app.services.audit import record_audit

Status = Literal[
    "booked", "checked_in", "in_service", "completed", "cancelled", "no_show", "rescheduled"
]


def visible_to(user_id: uuid.UUID) -> ColumnElement[bool]:
    """The customer who owns it, or an owner or staff member of its business (FR-27)."""
    member = exists().where(
        BusinessMember.business_id == Appointment.business_id, BusinessMember.user_id == user_id
    )
    return or_(Appointment.customer_id == user_id, member)


async def cancel_appointment(
    session: AsyncSession, actor_id: uuid.UUID, appointment_id: uuid.UUID, now: datetime
) -> None:
    """One conditional UPDATE decides; a second read only explains why it changed nothing.
    Strangers get the same 404 as a missing id, since appointment ids are not public."""
    cancelled = (
        await session.execute(
            update(Appointment)
            .where(
                Appointment.id == appointment_id,
                Appointment.status == "booked",
                func.lower(Appointment.during) > now,
                visible_to(actor_id),
            )
            .values(status="cancelled")
            .returning(Appointment.business_id, Appointment.customer_id)
        )
    ).one_or_none()
    if cancelled is not None:
        if cancelled.customer_id != actor_id:  # staff and owner cancellations are audited
            record_audit(
                session,
                business_id=cancelled.business_id,
                actor_id=actor_id,
                action="appointment.cancel",
                entity_type="appointment",
                entity_id=appointment_id,
                data={"customer_id": str(cancelled.customer_id)},
            )
        await emit_event(
            session,
            business_id=cancelled.business_id,
            queue_id=None,
            event_type="appointment.cancelled",
            payload={"appointment_id": str(appointment_id), "status": "cancelled"},
            at=now,
        )
        await session.commit()
        return
    status = await session.scalar(
        select(Appointment.status).where(Appointment.id == appointment_id, visible_to(actor_id))
    )
    if status is None:
        raise NotFound("Appointment not found")
    if status != "cancelled":  # cancelling twice is a no-op (204)
        raise InvalidTransition(
            "Only an appointment that has not started and is booked can be cancelled"
        )


async def find_own(
    session: AsyncSession, appointment_id: uuid.UUID, customer_id: uuid.UUID
) -> Appointment:
    """Another customer's appointment is a 404, the same as a missing one."""
    appointment = await session.scalar(
        select(Appointment).where(
            Appointment.id == appointment_id, Appointment.customer_id == customer_id
        )
    )
    if appointment is None:
        raise NotFound("Appointment not found")
    return appointment


def _cursor_start(cursor: str) -> tuple[datetime, uuid.UUID]:
    raw_start, appointment_id = decode_cursor(cursor)
    try:
        start = datetime.fromisoformat(raw_start)
    except ValueError:
        raise ValidationFailed(["query", "cursor"], "Invalid cursor") from None
    if start.tzinfo is None:
        raise ValidationFailed(["query", "cursor"], "Invalid cursor")
    return start, appointment_id


async def list_appointments(
    session: AsyncSession,
    *,
    customer_id: uuid.UUID | None,
    business_id: uuid.UUID | None,
    status: Status | None,
    start_from: datetime | None,
    start_to: datetime | None,
    cursor: str | None,
    limit: int,
) -> AppointmentPage | StaffAppointmentPage:
    """One query with joins (no N+1). Exactly one of customer_id (own appointments) and
    business_id (staff view, membership already checked) is set; both filter the query."""
    start = func.lower(Appointment.during)
    query = (
        select(
            Appointment,
            Service.name.label("service_name"),
            Service.duration_min,
            Business.timezone,
            User.id.label("customer_user_id"),
            User.name.label("customer_name"),
        )
        .join(Service, Service.id == Appointment.service_id)
        .join(Business, Business.id == Appointment.business_id)
        .join(User, User.id == Appointment.customer_id)
        .order_by(start, Appointment.id)
        .limit(limit + 1)
    )
    if customer_id is not None:
        query = query.where(Appointment.customer_id == customer_id)
    if business_id is not None:
        query = query.where(Appointment.business_id == business_id)
    if status:
        query = query.where(Appointment.status == status)
    if start_from:
        query = query.where(start >= start_from)
    if start_to:
        query = query.where(start < start_to)
    if cursor:
        query = query.where(tuple_(start, Appointment.id) > tuple_(*_cursor_start(cursor)))
    rows = (await session.execute(query)).all()
    page, has_more = rows[:limit], len(rows) > limit
    next_cursor = None
    if has_more:
        last = page[-1].Appointment
        next_cursor = encode_cursor(last.during.lower.astimezone(UTC).isoformat(), last.id)
    items = [_display(row) for row in page]
    if business_id is None:
        return AppointmentPage(items=items, next_cursor=next_cursor)
    return StaffAppointmentPage(
        items=[
            StaffAppointmentOut(
                **item.model_dump(),
                customer=CustomerOut(id=row.customer_user_id, name=row.customer_name),
            )
            for item, row in zip(items, page, strict=True)
        ],
        next_cursor=next_cursor,
    )


ListRow = Row[Appointment, str, int, str, uuid.UUID, str]


def _display(row: ListRow) -> AppointmentOut:
    appointment: Appointment = row.Appointment
    zone = get_zone(row.timezone)
    start = appointment.during.lower
    assert start is not None  # range_valid forbids unbounded ranges
    duration = timedelta(minutes=row.duration_min)
    return AppointmentOut(
        id=appointment.id,
        business_id=appointment.business_id,
        service_id=appointment.service_id,
        service_name=row.service_name,
        resource_id=appointment.resource_id,
        start=start.astimezone(zone).isoformat(),
        end=(start + duration).astimezone(zone).isoformat(),
        status=appointment.status,
        created_at=appointment.created_at.astimezone(zone).isoformat(),
    )
