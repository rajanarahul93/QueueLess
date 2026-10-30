import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, ValidationFailed
from app.core.timezones import get_zone
from app.models import Appointment, Business, Resource, Service
from app.models.appointment import ACTIVE_STATUSES
from app.schemas.appointment import AvailabilityOut, SlotOut

GRID_STEP = timedelta(minutes=15)
MAX_DAYS_AHEAD = 60
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
    today = now.astimezone(zone).date()
    if day < today:
        raise ValidationFailed(["query", "date"], "Date is in the past")
    if day > today + timedelta(days=MAX_DAYS_AHEAD):
        raise ValidationFailed(["query", "date"], f"At most {MAX_DAYS_AHEAD} days ahead")
    return day


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
    resource = await session.scalar(
        select(Resource)
        .where(Resource.business_id == business.id, Resource.is_active)
        .order_by(Resource.name, Resource.id)
        .limit(1)
    )
    if resource is None:
        raise NotFound("No active resource")
    day_hours: list[dict[str, Any]] = business.open_hours.get(WEEKDAYS[day.weekday()], [])
    intervals = expand_day(day_hours, day, zone)
    duration = timedelta(minutes=service.duration_min)
    buffer = timedelta(minutes=service.buffer_min)
    slots: list[Slot] = []
    if intervals:
        lower = min(i.start for i in intervals)
        upper = max(i.end for i in intervals) + buffer
        busy = await _busy_ranges(session, resource.id, lower, upper)
        slots = free_slots(intervals, duration, buffer, busy, now)
    return AvailabilityOut(
        timezone=business.timezone,
        slots=[
            SlotOut(
                start=s.start.astimezone(zone).isoformat(),
                end=s.end.astimezone(zone).isoformat(),
                resource_id=resource.id,
            )
            for s in slots
        ],
    )


async def _busy_ranges(
    session: AsyncSession, resource_id: uuid.UUID, lower: datetime, upper: datetime
) -> list[tuple[datetime, datetime]]:
    """One query for the whole day; stored ranges already include each appointment's buffer."""
    rows = await session.scalars(
        select(Appointment.during).where(
            Appointment.resource_id == resource_id,
            Appointment.status.in_(ACTIVE_STATUSES),
            Appointment.during.overlaps(Range(lower, upper, bounds="[)")),
        )
    )
    return [(r.lower, r.upper) for r in rows if r.lower is not None and r.upper is not None]
