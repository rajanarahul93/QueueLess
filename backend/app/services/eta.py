"""Expected durations and ETA ranges (FR-14, FR-15). Pure: seconds in, minutes out; the queue
service gathers the session samples.

A token's expected duration takes the first tier with enough samples: the service in this
business-local hour, the service at any hour, then the service's configured duration. It is
blended with today's sessions once there are a few. A waiting token's ETA is the time left on
the sessions in progress plus the expected durations of every token served before it, divided by
the resources serving the queue. The range adds the p25 and p75 spreads of those durations by
root-sum-square (independent durations spread like the square root of their count): an estimate,
not a guarantee.
"""

import math
import statistics
from collections.abc import Hashable, Sequence
from dataclasses import dataclass

LAST_SESSIONS = 50
MIN_TIER_SAMPLES = 10
MIN_TODAY_SAMPLES = 3
TRIM_SHARE = 0.1  # dropped from each end
HISTORY_WEIGHT = 0.7  # today's weight is the rest
MIN_SESSION_SEC = 30  # shorter or longer sessions are left out of every sample
MAX_SESSION_FACTOR = 4  # times the service's configured duration (forgotten tokens)
OVERRUN_FLOOR_SEC = 60  # a session past its expected end still has this much left
FALLBACK_SPREAD = 0.2  # the configured duration has no samples: p25 and p75 at -/+ 20%
MIN_WIDTH_MIN = 2


@dataclass(frozen=True)
class Duration:
    """Expected seconds, and how far p25 lies below and p75 above it."""

    mean: float
    below: float
    above: float


@dataclass(frozen=True)
class Eta:
    min_minutes: int
    max_minutes: int
    wait_sec: int  # the expected wait itself, before rounding (stored as predicted_wait_sec)


def trimmed_mean(samples: Sequence[float]) -> float:
    ordered = sorted(samples)
    cut = math.floor(len(ordered) * TRIM_SHARE)
    kept = ordered[cut : len(ordered) - cut]
    return sum(kept) / len(kept)


def blend(historical: float, today: Sequence[float]) -> float:
    if len(today) < MIN_TODAY_SAMPLES:
        return historical
    return HISTORY_WEIGHT * historical + (1 - HISTORY_WEIGHT) * trimmed_mean(today)


def expected(
    tiers: Sequence[Sequence[float]], configured_sec: float, today: Sequence[float]
) -> Duration:
    """The first tier with MIN_TIER_SAMPLES samples, else the configured duration; then blended."""
    for samples in tiers:
        if len(samples) >= MIN_TIER_SAMPLES:
            base = trimmed_mean(samples)
            p25, _, p75 = statistics.quantiles(samples, n=4, method="inclusive")
            break
    else:
        base = configured_sec
        p25, p75 = base * (1 - FALLBACK_SPREAD), base * (1 + FALLBACK_SPREAD)
    return Duration(blend(base, today), max(base - p25, 0.0), max(p75 - base, 0.0))


def remaining(expected_sec: float, elapsed_sec: float) -> float:
    return max(expected_sec - elapsed_sec, OVERRUN_FLOOR_SEC)


def round_minutes(seconds: float, up: bool) -> int:
    """Whole minutes under 10, steps of 5 up to 60, steps of 10 above, so the display is calm."""
    minutes = round(seconds / 60, 6)  # 1200 s is 20 minutes, not 20.000000001
    step = 1 if minutes < 10 else 5 if minutes <= 60 else 10
    return step * (math.ceil(minutes / step) if up else math.floor(minutes / step))


def estimate(centre_sec: float, below_sq: float, above_sq: float, resources: int) -> Eta:
    low = max(centre_sec - math.sqrt(below_sq), 0.0) / resources
    high = (centre_sec + math.sqrt(above_sq)) / resources
    low_min = round_minutes(low, up=False)
    return Eta(
        low_min,
        max(round_minutes(high, up=True), low_min + MIN_WIDTH_MIN),
        round(centre_sec / resources),
    )


def queue_etas[Key: Hashable](
    remaining_sec: float,
    called: Sequence[Duration],
    waiting: Sequence[tuple[Key, Duration]],
    resources: int,
) -> dict[Key, Eta]:
    """`called` tokens are served before every waiting one; `waiting` is in call order."""
    centre = remaining_sec + sum(item.mean for item in called)
    below_sq = sum(item.below**2 for item in called)
    above_sq = sum(item.above**2 for item in called)
    etas: dict[Key, Eta] = {}
    for key, item in waiting:
        etas[key] = estimate(centre, below_sq, above_sq, resources)
        centre += item.mean
        below_sq += item.below**2
        above_sq += item.above**2
    return etas
