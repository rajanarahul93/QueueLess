"""ETA arithmetic against hand-computed numbers (worked out in the comments, not from the code)."""

import pytest

from app.services.eta import (
    Duration,
    Eta,
    blend,
    estimate,
    expected,
    queue_etas,
    remaining,
    round_minutes,
    trimmed_mean,
)

TEN = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]


def test_trimmed_mean_drops_ten_percent_from_each_end() -> None:
    # 10 samples: drop 1 each end -> 200..900, mean (200 + 900) / 2 = 550
    assert trimmed_mean(TEN) == 550
    # the outlier 5000 is the one dropped at the top: same 550
    assert trimmed_mean([*TEN[:-1], 5000]) == 550
    # 19 samples: floor(1.9) = 1 dropped each end; 1..19 -> 2..18, mean 10
    assert trimmed_mean(range(1, 20)) == 10
    # 20 samples: 2 dropped each end; 1..20 -> 3..18, mean 10.5
    assert trimmed_mean(range(1, 21)) == 10.5
    # under 10 samples nothing is dropped: (10 + 20 + 90) / 3 = 40
    assert trimmed_mean([90, 10, 20]) == 40


def test_the_first_tier_with_ten_samples_wins() -> None:
    nine = [60.0] * 9
    # p25 and p75 of TEN (inclusive, like percentile_cont): positions 2.25 and 6.75 -> 325, 775
    # spread from the trimmed mean 550: 225 below, 225 above
    assert expected([nine, TEN], 900, []) == Duration(550, 225, 225)
    assert expected([[*nine, 60.0], TEN], 900, []) == Duration(60, 0, 0)  # 10 samples: tier 1
    # no tier has 10: the configured 900 s with -/+ 20%: 180 below, 180 above
    assert expected([nine, nine], 900, []) == Duration(900, 180, 180)


def test_the_blend_needs_three_sessions_today() -> None:
    # 0.7 * 550 + 0.3 * mean(300, 600, 900) = 385 + 180 = 565
    assert blend(550, [300, 600, 900]) == pytest.approx(565)
    assert blend(550, [300, 600]) == 550
    # the spread stays the tier's; only the centre moves
    blended = expected([TEN], 900, [300, 600, 900])
    assert blended.mean == pytest.approx(565)
    assert (blended.below, blended.above) == (225, 225)
    # the configured tier blends too: 0.7 * 900 + 0.3 * 600 = 810
    assert expected([], 900, [300, 600, 900]).mean == pytest.approx(810)


@pytest.mark.parametrize(
    ("seconds", "down", "up"),
    [
        (0, 0, 0),
        (9.5 * 60, 9, 10),  # under 10: whole minutes
        (10 * 60, 10, 10),  # 10 to 60: steps of 5
        (17.2 * 60, 15, 20),
        (20 * 60, 20, 20),
        (60 * 60, 60, 60),
        (61 * 60, 60, 70),  # over 60: steps of 10
        (95 * 60, 90, 100),
    ],
)
def test_rounding_bands(seconds: float, down: int, up: int) -> None:
    assert (round_minutes(seconds, up=False), round_minutes(seconds, up=True)) == (down, up)


def test_root_sum_square_range() -> None:
    item = Duration(600, 120, 120)
    # third token: 2 x 600 = 1200 s ahead; spread sqrt(120^2 + 120^2) = 169.7 s
    # low 1030.3 s = 17.2 min -> 15; high 1369.7 s = 22.8 min -> 25
    etas = queue_etas(0, [], [("a", item), ("b", item), ("c", item)], 1)
    assert etas["c"] == Eta(15, 25, 1200)
    # first token: nothing ahead -> 0 and the minimum width of 2 minutes
    assert etas["a"] == Eta(0, 2, 0)
    # second: 600 +/- 120 s -> 8 to 12 min; 8 is under 10 (whole), 12 rounds up to 15
    assert etas["b"] == Eta(8, 15, 600)


def test_called_tokens_and_the_session_in_progress_come_first() -> None:
    # 300 s left on the session, one called token of 600 s (+/- 0): first waiting at 900 s = 15
    etas = queue_etas(300, [Duration(600, 0, 0)], [("a", Duration(600, 0, 0))], 1)
    assert etas["a"] == Eta(15, 17, 900)


def test_two_resources_halve_the_wait() -> None:
    # 1200 s ahead over 2 resources = 600 s = 10 min; no spread -> 10 to 12
    etas = queue_etas(0, [Duration(1200, 0, 0)], [("a", Duration(60, 0, 0))], 2)
    assert etas["a"] == Eta(10, 12, 600)


def test_an_overrunning_session_keeps_a_floor() -> None:
    assert remaining(600, 100) == 500
    assert remaining(600, 590) == 60
    assert remaining(600, 5000) == 60


def test_an_empty_queue_has_no_etas() -> None:
    assert queue_etas(0, [], [], 1) == {}


def test_estimate_never_goes_below_zero() -> None:
    # centre 60 s, spread 600 s below: low clamps to 0
    assert estimate(60, 600**2, 0, 1) == Eta(0, 2, 60)
