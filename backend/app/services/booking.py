import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import Row, func, insert, select, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    BookingLimitReached,
    BookingRestricted,
    InvalidTransition,
    NotFound,
    RescheduleCutoff,
    ServiceUnavailable,
    SlotTaken,
    ValidationFailed,
)
from app.core.timezones import get_zone
from app.models import Appointment, Business, Resource, Service, User
from app.models.appointment import ACTIVE_STATUSES
from app.schemas.appointment import AppointmentOut, AvailabilityOut, SlotOut
from app.services.queue import default_resource

GRID_STEP = timedelta(minutes=15)
MAX_DAYS_AHEAD = 60
NO_SHOW_BOOKING_LIMIT = 3  # PRD D6: this many no-shows block advance booking
MAX_ACTIVE_BOOKINGS = 5  # FR-34: active appointments per customer that end in the future
NEXT_SLOT_COUNT = 3
DEADLOCK_RETRIES = 2
EXCLUSION_VIOLATION = "23P01"
DEADLOCK_DETECTED = "40P01"
OVERLAP_CONSTRAINT = "no_overlap"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Interval:
    """An open interval in UTC plus the grid start times inside it (all UTC)."""

    start: datetime
    end: datetime
    starts: tuple[datetime, ...]


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime  # start + service duration; the buffer is not part of the visible slot


def _to_utc(local: datetime, zone: ZoneInfo) -> datetime | None:
    """None for a wall time that does not exist (spring-forward gap). fold=0 picks the first
    occurrence of an ambiguous time (fall-back)."""
    aware = local.replace(tzinfo=zone, fold=0)
    utc = aware.astimezone(UTC)
    if utc.astimezone(zone).replace(tzinfo=None) != local:
        return None
    return utc


def _wall(day: date, hhmm: str) -> datetime:
    # open_hours cannot say 24:00, so "23:59" means the end of the day.
    if hhmm == "23:59":
        return datetime.combine(day + timedelta(days=1), time.min)
    hour, minute = hhmm.split(":")
    return datetime.combine(day, time(int(hour), int(minute)))


def expand_day(
    day_hours: Sequence[dict[str, str]], day: date, zone: ZoneInfo, step: timedelta = GRID_STEP
) -> list[Interval]:
    """Open intervals of one local day. The grid steps in wall-clock time from each opening, so
    nonexistent local times are skipped and a repeated local time appears only once."""
    intervals: list[Interval] = []
    for hours in day_hours:
        open_local, close_local = _wall(day, hours["open"]), _wall(day, hours["close"])
        # An opening or closing inside a gap resolves forward (astimezone of fold=0).
        start = open_local.replace(tzinfo=zone, fold=0).astimezone(UTC)
        end = close_local.replace(tzinfo=zone, fold=0).astimezone(UTC)
        starts: list[datetime] = []
        cursor = open_local
        while cursor < close_local:
            utc = _to_utc(cursor, zone)
            if utc is not None:
                starts.append(utc)
            cursor += step
        intervals.append(Interval(start, end, tuple(starts)))
    return intervals


def free_slots(
    intervals: Sequence[Interval],
    duration: timedelta,
    buffer: timedelta,
    busy: Sequence[tuple[datetime, datetime]],
    now: datetime,
) -> list[Slot]:
    """Pure. The service time [start, start + duration) must fit inside one interval; the stored
    range [start, start + duration + buffer) must not overlap a busy (stored) range. The buffer
    may run past closing time. Busy ranges are half-open, so back-to-back is allowed."""
    slots: list[Slot] = []
    for interval in intervals:
        for start in interval.starts:
            end = start + duration
            if start <= now or end > interval.end:
                continue
            stored_end = end + buffer
            if any(b_start < stored_end and start < b_end for b_start, b_end in busy):
                continue
            slots.append(Slot(start, end))
    return sorted(slots, key=lambda slot: slot.start)


def parse_day(raw: str | None, zone: ZoneInfo, now: datetime) -> date:
    """`date` is in the business time zone; today is allowed, the past and 60+ days are not."""
    try:
        if raw is None or len(raw) != 10:
            raise ValueError
        day = date.fromisoformat(raw)
    except ValueError:
        raise ValidationFailed(["query", "date"], "Use YYYY-MM-DD") from None
    check_day(day, zone, now, ["query", "date"])
    return day


def check_day(day: date, zone: ZoneInfo, now: datetime, loc: list[str]) -> None:
    today = now.astimezone(zone).date()
    if day < today:
        raise ValidationFailed(loc, "Date is in the past")
    if day > today + timedelta(days=MAX_DAYS_AHEAD):
        raise ValidationFailed(loc, f"At most {MAX_DAYS_AHEAD} days ahead")


async def find_service(
    session: AsyncSession, business_id: uuid.UUID, raw_service_id: str | None
) -> Service:
    """404 for an unknown, inactive or foreign service, and for a malformed id (cannot match)."""
    try:
        service_id = uuid.UUID(raw_service_id or "")
    except ValueError:
        raise NotFound("Service not found") from None
    service = await session.scalar(
        select(Service).where(
            Service.id == service_id, Service.business_id == business_id, Service.is_active
        )
    )
    if service is None:
        raise NotFound("Service not found")
    return service


async def get_availability(
    session: AsyncSession,
    business: Business,
    service: Service,
    raw_day: str | None,
    clock: Callable[[], float],
) -> AvailabilityOut:
    now = datetime.fromtimestamp(clock(), UTC)
    zone = get_zone(business.timezone)
    day = parse_day(raw_day, zone, now)
    resource = await default_resource(session, business.id)
    slots = await day_slots(session, business, service, resource.id, day, now)
    return AvailabilityOut(
        timezone=business.timezone, slots=[slot_out(s, zone, resource.id) for s in slots]
    )


def slot_out(slot: Slot, zone: ZoneInfo, resource_id: uuid.UUID) -> SlotOut:
    return SlotOut(
        start=slot.start.astimezone(zone).isoformat(),
        end=slot.end.astimezone(zone).isoformat(),
        resource_id=resource_id,
    )


async def day_slots(
    session: AsyncSession,
    business: Business,
    service: Service,
    resource_id: uuid.UUID,
    day: date,
    now: datetime,
    exclude_id: uuid.UUID | None = None,
) -> list[Slot]:
    zone = get_zone(business.timezone)
    day_hours: list[dict[str, Any]] = business.open_hours.get(WEEKDAYS[day.weekday()], [])
    intervals = expand_day(day_hours, day, zone)
    if not intervals:
        return []
    buffer = timedelta(minutes=service.buffer_min)
    lower = min(i.start for i in intervals)
    upper = max(i.end for i in intervals) + buffer
    busy = await _busy_ranges(session, resource_id, lower, upper, exclude_id)
    return free_slots(intervals, timedelta(minutes=service.duration_min), buffer, busy, now)


async def _busy_ranges(
    session: AsyncSession,
    resource_id: uuid.UUID,
    lower: datetime,
    upper: datetime,
    exclude_id: uuid.UUID | None = None,
) -> list[tuple[datetime, datetime]]:
    """One query for the whole day; stored ranges already include each appointment's buffer."""
    query = select(Appointment.during).where(
        Appointment.resource_id == resource_id,
        Appointment.status.in_(ACTIVE_STATUSES),
        Appointment.during.overlaps(Range(lower, upper, bounds="[)")),
    )
    if exclude_id is not None:
        query = query.where(Appointment.id != exclude_id)
    rows = await session.scalars(query)
    return [(r.lower, r.upper) for r in rows if r.lower is not None and r.upper is not None]


@dataclass(frozen=True)
class BookingTarget:
    business: Business
    service: Service
    resource: Resource
    start: datetime


async def find_bookable(
    session: AsyncSession, service_id: uuid.UUID, resource_id: uuid.UUID, start: datetime
) -> BookingTarget:
    """404 for an unknown, inactive or foreign service or resource. The business comes from the
    service, so a resource of another business can never match."""
    row = (
        await session.execute(
            select(Service, Business)
            .join(Business, Business.id == Service.business_id)
            .where(Service.id == service_id, Service.is_active)
        )
    ).one_or_none()
    if row is None:
        raise NotFound("Service not found")
    service, business = row
    resource = await session.scalar(
        select(Resource).where(
            Resource.id == resource_id, Resource.business_id == business.id, Resource.is_active
        )
    )
    if resource is None:
        raise NotFound("Resource not found")
    # Detached, so the rollback in book_appointment cannot expire what the retry still reads.
    session.expunge_all()
    return BookingTarget(business, service, resource, start)


def check_start(target: BookingTarget, now: datetime) -> None:
    """Deterministic rules only; busy ranges are never read before the insert (R3.1)."""
    zone = get_zone(target.business.timezone)
    loc = ["body", "start"]
    local_day = target.start.astimezone(zone).date()
    check_day(local_day, zone, now, loc)
    if target.start <= now:
        raise ValidationFailed(loc, "Start is in the past")
    day_hours: list[dict[str, Any]] = target.business.open_hours.get(
        WEEKDAYS[local_day.weekday()], []
    )
    duration = timedelta(minutes=target.service.duration_min)
    offered = free_slots(expand_day(day_hours, local_day, zone), duration, timedelta(0), [], now)
    if target.start not in {slot.start for slot in offered}:
        raise ValidationFailed(loc, "Start is not an available time")


def classify_pg_error(error: DBAPIError) -> Literal["overlap", "deadlock"] | None:
    """Reads the driver's SQLSTATE and constraint name, never the message text."""
    cause = error.orig.__cause__ if error.orig is not None else None
    sqlstate = getattr(cause, "sqlstate", None)
    if sqlstate == EXCLUSION_VIOLATION and getattr(cause, "constraint_name", None) == (
        OVERLAP_CONSTRAINT
    ):
        return "overlap"
    if sqlstate == DEADLOCK_DETECTED:
        return "deadlock"
    return None


async def book_appointment(
    session: AsyncSession,
    customer_id: uuid.UUID,
    target: BookingTarget,
    clock: Callable[[], float],
    replaces: uuid.UUID | None = None,
) -> AppointmentOut:
    """Books `target`; with `replaces` it is a reschedule of that appointment (FR-19), in the
    same transaction and the same lock order: customer row, then the old appointment."""
    check_start(target, datetime.fromtimestamp(clock(), UTC))
    for attempt in range(DEADLOCK_RETRIES + 1):
        now = datetime.fromtimestamp(clock(), UTC)
        try:
            return await _book_once(session, customer_id, target, now, replaces)
        except _InsertFailed as failure:
            await session.rollback()  # a reschedule's old appointment is exactly as it was
            if failure.kind == "overlap":
                raise SlotTaken(await _next_slots(session, target, clock, replaces)) from None
            # A deadlock aborts this transaction only; the retry sees the winner's committed row
            # and fails cleanly with an exclusion violation.
            if attempt == DEADLOCK_RETRIES:
                raise ServiceUnavailable("Please retry", retry_after_sec=1) from None
    raise AssertionError("unreachable")  # pragma: no cover


class _InsertFailed(Exception):
    def __init__(self, kind: Literal["overlap", "deadlock"]) -> None:
        self.kind = kind


async def _book_once(
    session: AsyncSession,
    customer_id: uuid.UUID,
    target: BookingTarget,
    now: datetime,
    replaces: uuid.UUID | None,
) -> AppointmentOut:
    service = target.service
    # Locking the customer row serializes this customer's bookings, so the cap is exact. NO KEY
    # UPDATE does not block other transactions' foreign-key checks on the row.
    no_shows = await session.scalar(
        select(User.no_show_count).where(User.id == customer_id).with_for_update(key_share=True)
    )
    if replaces is not None:
        await _check_replaced(session, customer_id, replaces, target, now)
    if no_shows is not None and no_shows >= NO_SHOW_BOOKING_LIMIT:
        raise BookingRestricted("Booking is restricted after repeated no-shows")
    await _check_cap(session, customer_id, now, replaces)
    if replaces is not None:
        # Marked first: the constraint ignores rescheduled rows, so the new range may overlap
        # the one it replaces. A failed insert rolls this back with everything else.
        await session.execute(
            update(Appointment).where(Appointment.id == replaces).values(status="rescheduled")
        )
    row = await _insert(session, customer_id, target, replaces)
    # Phase 4: the outbox event for a booking or reschedule is written here, before the commit.
    await session.commit()
    zone = get_zone(target.business.timezone)
    return AppointmentOut(
        id=row.id,
        business_id=service.business_id,
        service_id=service.id,
        service_name=service.name,
        resource_id=target.resource.id,
        start=target.start.astimezone(zone).isoformat(),
        end=(target.start + timedelta(minutes=service.duration_min)).astimezone(zone).isoformat(),
        status="booked",
        created_at=row.created_at.astimezone(zone).isoformat(),
    )


async def _check_replaced(
    session: AsyncSession,
    customer_id: uuid.UUID,
    replaces: uuid.UUID,
    target: BookingTarget,
    now: datetime,
) -> None:
    """Locks the old appointment (after the customer row) and checks state, start and cutoff."""
    old = (
        await session.execute(
            select(func.lower(Appointment.during).label("start"), Appointment.status)
            .where(Appointment.id == replaces, Appointment.customer_id == customer_id)
            .with_for_update()
        )
    ).one_or_none()
    if old is None:
        raise NotFound("Appointment not found")
    old_start: datetime = old.start
    if old.status != "booked":
        raise InvalidTransition("Only a booked appointment can be rescheduled")
    if old_start <= now:
        raise InvalidTransition("The appointment has already started")
    cutoff = timedelta(minutes=target.business.settings["reschedule_cutoff_min"])
    if old_start - now < cutoff:
        raise RescheduleCutoff("Too close to the appointment to reschedule")
    if old_start == target.start:
        raise ValidationFailed(["body", "start"], "same time")


async def _check_cap(
    session: AsyncSession, customer_id: uuid.UUID, now: datetime, replaces: uuid.UUID | None
) -> None:
    """The appointment being replaced does not count, so a reschedule is net zero."""
    query = (
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.customer_id == customer_id,
            Appointment.status.in_(ACTIVE_STATUSES),
            func.upper(Appointment.during) > now,
        )
    )
    if replaces is not None:
        query = query.where(Appointment.id != replaces)
    if (await session.scalar(query) or 0) >= MAX_ACTIVE_BOOKINGS:
        raise BookingLimitReached(f"At most {MAX_ACTIVE_BOOKINGS} active appointments")


async def _insert(
    session: AsyncSession,
    customer_id: uuid.UUID,
    target: BookingTarget,
    replaces: uuid.UUID | None,
) -> Row[uuid.UUID, datetime]:
    service = target.service
    stored_end = target.start + timedelta(minutes=service.duration_min + service.buffer_min)
    try:  # only the insert is classified; anything else propagates
        return (
            await session.execute(
                insert(Appointment)
                .values(
                    business_id=service.business_id,
                    service_id=service.id,
                    resource_id=target.resource.id,
                    customer_id=customer_id,
                    during=Range(target.start, stored_end, bounds="[)"),
                    status="booked",
                    rescheduled_from_id=replaces,
                )
                .returning(Appointment.id, Appointment.created_at)
            )
        ).one()
    except DBAPIError as error:
        kind = classify_pg_error(error)
        if kind is None:
            raise
        raise _InsertFailed(kind) from error


async def _next_slots(
    session: AsyncSession,
    target: BookingTarget,
    clock: Callable[[], float],
    replaces: uuid.UUID | None,
) -> list[dict[str, object]]:
    """A new read after the rollback: up to 3 later slots on the same local date. A reschedule
    could move into its own old range, so that range is not counted as busy."""
    zone = get_zone(target.business.timezone)
    day = target.start.astimezone(zone).date()
    slots = await day_slots(
        session,
        target.business,
        target.service,
        target.resource.id,
        day,
        datetime.fromtimestamp(clock(), UTC),
        replaces,
    )
    later = [slot for slot in slots if slot.start > target.start][:NEXT_SLOT_COUNT]
    return [slot_out(slot, zone, target.resource.id).model_dump(mode="json") for slot in later]
