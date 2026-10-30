import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.core.timezones import get_zone, zone_names

APP = Path(__file__).resolve().parents[2] / "app"


def offset(
    name: str, year: int, month: int, day: int, hour: int, minute: int, fold: int = 0
) -> timedelta:
    delta = datetime(year, month, day, hour, minute, fold=fold, tzinfo=get_zone(name)).utcoffset()
    assert delta is not None
    return delta


def hours(value: float) -> timedelta:
    return timedelta(hours=value)


def test_real_zones_are_known_and_junk_names_are_not() -> None:
    names = zone_names()
    assert {"Asia/Kolkata", "America/New_York", "Europe/London", "UTC"} <= names
    for junk in (
        "localtime",
        "posixrules",
        "Factory",
        "posix/UTC",
        "right/UTC",
        "../etc/passwd",
        "",
    ):
        assert junk not in names
        with pytest.raises(ValueError):
            get_zone(junk)


def test_get_zone_is_cached_keyed_and_not_the_host_object() -> None:
    zone = get_zone("Asia/Kolkata")
    assert get_zone("Asia/Kolkata") is zone
    assert zone.key == "Asia/Kolkata"
    # ZoneInfo(name) is the host-backed, process-cached object; ours is loaded from the package.
    assert zone is not ZoneInfo("Asia/Kolkata")


def test_unreadable_index_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    class Empty:
        def joinpath(self, name: str) -> "Empty":
            return self

        def read_text(self) -> str:
            return "\n"

    monkeypatch.setattr("importlib.resources.files", lambda package: Empty())
    zone_names.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="empty"):
            zone_names()
    finally:
        monkeypatch.undo()
        zone_names.cache_clear()
    assert "Asia/Kolkata" in zone_names()


def test_new_york_dst_2026() -> None:
    assert offset("America/New_York", 2026, 3, 8, 1, 59) == -hours(5)
    assert offset("America/New_York", 2026, 3, 8, 3, 0) == -hours(4)
    assert offset("America/New_York", 2026, 11, 1, 1, 30, fold=0) == -hours(4)
    assert offset("America/New_York", 2026, 11, 1, 1, 30, fold=1) == -hours(5)
    assert offset("America/New_York", 2026, 11, 1, 3, 0) == -hours(5)


def test_london_dst_2026() -> None:
    assert offset("Europe/London", 2026, 3, 29, 0, 59) == hours(0)
    assert offset("Europe/London", 2026, 3, 29, 2, 0) == hours(1)
    assert offset("Europe/London", 2026, 10, 25, 0, 30) == hours(1)
    assert offset("Europe/London", 2026, 10, 25, 3, 0) == hours(0)


def test_kolkata_is_plus_0530_all_year() -> None:
    for month in (1, 4, 7, 10):
        assert offset("Asia/Kolkata", 2026, month, 15, 12, 0) == timedelta(hours=5, minutes=30)


def test_dst_instants_are_correct_in_utc() -> None:
    new_york = get_zone("America/New_York")
    assert datetime(2026, 3, 8, 9, 0, tzinfo=new_york).astimezone(UTC).hour == 13
    assert datetime(2026, 11, 1, 9, 0, tzinfo=new_york).astimezone(UTC).hour == 14


def test_nothing_in_app_reads_the_host_tz_files() -> None:
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        if path.name == "timezones.py" and path.parent.name == "core":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            call = isinstance(node, ast.Call) and (
                getattr(node.func, "id", None) == "ZoneInfo"
                or getattr(node.func, "attr", None) == "ZoneInfo"
            )
            listing = isinstance(node, ast.Attribute | ast.Name) and "available_timezones" in (
                getattr(node, "attr", None) or getattr(node, "id", None) or ""
            )
            if call or listing:
                offenders.append(f"{path.relative_to(APP)}:{node.lineno}")  # type: ignore[attr-defined]
    assert offenders == []
