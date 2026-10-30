from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.errors import ValidationFailed
from app.core.timezones import get_zone
from app.services.booking import Interval, expand_day, free_slots, parse_day

UTC_ZONE = get_zone("UTC")
DAY = date(2026, 11, 2)
LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


def at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def starts(slots: list) -> list[str]:  # type: ignore[type-arg]
    return [s.start.strftime("%H:%M") for s in slots]


def intervals(*spans: tuple[str, str]) -> list[Interval]:
    return expand_day([{"open": o, "close": c} for o, c in spans], DAY, UTC_ZONE)


def minutes(value: int) -> timedelta:
    return timedelta(minutes=value)


def run(
    spans: list[tuple[str, str]],
    duration: int,
    buffer: int = 0,
    busy: list[tuple[datetime, datetime]] | None = None,
    now: datetime = LONG_AGO,
) -> list[str]:
    found = free_slots(intervals(*spans), minutes(duration), minutes(buffer), busy or [], now)
    return starts(found)


def test_grid_is_every_15_minutes_and_service_must_fit() -> None:
    slots = free_slots(intervals(("09:00", "10:00")), minutes(30), minutes(0), [], LONG_AGO)
    assert starts(slots) == ["09:00", "09:15", "09:30"]
    assert slots[0].end == at(9, 30)  # the visible slot ends at start + duration


def test_exact_fit_and_one_minute_too_long() -> None:
    assert starts(
        free_slots(intervals(("09:00", "10:00")), minutes(60), minutes(0), [], LONG_AGO)
    ) == ["09:00"]
    assert free_slots(intervals(("09:00", "10:00")), minutes(61), minutes(0), [], LONG_AGO) == []


def test_buffer_may_run_past_closing_but_service_may_not() -> None:
    slots = free_slots(intervals(("09:00", "17:00")), minutes(30), minutes(10), [], LONG_AGO)
    assert "16:30" in starts(slots)  # service ends exactly 17:00, stored range ends 17:10
    assert "16:45" not in starts(slots)  # service would end 17:15


def test_trailing_buffer_must_not_overlap_the_next_appointment() -> None:
    busy = [(at(11, 0), at(11, 40))]  # 30-minute appointment plus a 10-minute buffer
    slots = starts(
        free_slots(intervals(("09:00", "17:00")), minutes(30), minutes(10), busy, LONG_AGO)
    )
    assert "10:15" in slots  # stored range ends 10:55
    assert "10:30" not in slots  # stored range ends 11:10, into the next appointment
    assert "11:30" not in slots  # inside the appointment's own buffer
    assert "11:45" in slots  # offered once that buffer has ended


def test_back_to_back_is_allowed_and_overlap_is_not() -> None:
    busy = [(at(10, 0), at(10, 30))]
    slots = run([("09:00", "11:00")], 30, 0, busy)
    # 09:30 ends exactly when the busy range starts; 09:45 would run into it
    assert slots == ["09:00", "09:15", "09:30", "10:30"]


def test_overlapping_busy_ranges_block_everything_they_cover() -> None:
    busy = [(at(9, 0), at(10, 0)), (at(9, 30), at(10, 30))]
    slots = starts(
        free_slots(intervals(("09:00", "11:00")), minutes(30), minutes(0), busy, LONG_AGO)
    )
    assert slots == ["10:30"]


def test_past_slots_are_dropped_and_now_itself_is_not_offered() -> None:
    slots = free_slots(intervals(("09:00", "10:00")), minutes(15), minutes(0), [], at(9, 15))
    assert starts(slots) == ["09:30", "09:45"]


def test_empty_day_and_two_intervals() -> None:
    assert free_slots([], minutes(30), minutes(0), [], LONG_AGO) == []
    two = starts(
        free_slots(
            intervals(("09:00", "09:30"), ("13:00", "13:45")), minutes(30), minutes(0), [], LONG_AGO
        )
    )
    assert two == ["09:00", "13:00", "13:15"]


def test_2359_means_end_of_day() -> None:
    slots = free_slots(intervals(("22:00", "23:59")), minutes(30), minutes(0), [], LONG_AGO)
    assert starts(slots)[-1] == "23:30"
    assert slots[-1].end == at(0, 0, DAY + timedelta(days=1))
    assert "23:45" not in starts(slots)


def test_spring_forward_skips_the_gap_and_counts_elapsed_time() -> None:
    ny = get_zone("America/New_York")
    day = date(2026, 3, 8)
    (interval,) = expand_day([{"open": "00:00", "close": "06:00"}], day, ny)
    assert len(interval.starts) == 20  # 24 wall-clock steps minus the nonexistent 02:00-02:59
    assert interval.end - interval.start == timedelta(hours=5)
    local = [s.astimezone(ny).strftime("%H:%M") for s in interval.starts]
    assert not any(t.startswith("02:") for t in local)
    assert "03:00" in local


def test_fall_back_offers_each_wall_time_once() -> None:
    ny = get_zone("America/New_York")
    day = date(2026, 11, 1)
    (interval,) = expand_day([{"open": "00:00", "close": "06:00"}], day, ny)
    assert len(interval.starts) == 24
    assert interval.end - interval.start == timedelta(hours=7)
    keys = [s.astimezone(ny).strftime("%H:%M") for s in interval.starts]
    assert len(set(keys)) == 24  # no duplicate local times
    first_pass = interval.starts[keys.index("01:15")]
    assert first_pass.astimezone(ny).utcoffset() == -timedelta(hours=4)  # the first occurrence


def test_ordinary_days_around_dst_have_the_same_slot_count() -> None:
    ny = get_zone("America/New_York")
    for day, offset in ((date(2026, 3, 8), 13), (date(2026, 11, 1), 14)):
        (interval,) = expand_day([{"open": "09:00", "close": "17:00"}], day, ny)
        assert len(interval.starts) == 32
        assert interval.starts[0].hour == offset  # 09:00 local in UTC


def test_half_hour_offset_zone() -> None:
    kolkata = get_zone("Asia/Kolkata")
    (interval,) = expand_day([{"open": "09:00", "close": "10:00"}], DAY, kolkata)
    assert interval.start == datetime(2026, 11, 2, 3, 30, tzinfo=UTC)


def test_parse_day_rules() -> None:
    now = datetime(2026, 11, 2, 20, 0, tzinfo=UTC)  # 01:30 on 2026-11-03 in Kolkata
    kolkata = get_zone("Asia/Kolkata")
    assert parse_day("2026-11-03", kolkata, now) == date(2026, 11, 3)  # today, local
    assert parse_day("2027-01-02", kolkata, now) == date(2027, 1, 2)  # exactly 60 days ahead
    for bad in (None, "", "2026-1-3", "11/03/2026", "2026-13-01", "2026-11-02", "2027-01-03"):
        with pytest.raises(ValidationFailed):
            parse_day(bad, kolkata, now)
