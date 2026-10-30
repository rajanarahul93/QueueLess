"""Time zones from the packaged `tzdata` only, so every host applies the same DST rules.

Never call `ZoneInfo(...)` or `available_timezones()` elsewhere: they read the host's tz files,
which differ between machines (a test enforces this).
"""

from functools import cache, lru_cache
from importlib import resources
from zoneinfo import ZoneInfo

# Listed in the package index (Factory) or in some host directories, but not real places.
NOT_A_PLACE = frozenset({"localtime", "posixrules", "Factory"})


@lru_cache(maxsize=1)
def zone_names() -> frozenset[str]:
    """Raises if the package index is unreadable or empty, so a broken install fails at startup."""
    names = frozenset(resources.files("tzdata").joinpath("zones").read_text().split())
    if not names:
        raise RuntimeError("tzdata zone index is empty")
    return names - NOT_A_PLACE


@cache
def get_zone(name: str) -> ZoneInfo:
    if name not in zone_names():
        raise ValueError(f"unknown IANA timezone: {name!r}")
    with resources.files("tzdata").joinpath(f"zoneinfo/{name}").open("rb") as handle:
        return ZoneInfo.from_file(handle, key=name)
