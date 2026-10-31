"""When a token's projected delay calls for a running-late event (threshold 10, hysteresis 5)."""

import pytest

from app.services.queue_jobs import RUNNING_LATE, RUNNING_LATE_CLEARED, late_change


@pytest.mark.parametrize(
    ("late", "delay_min", "event"),
    [
        (False, 9, None),
        (False, 10, RUNNING_LATE),  # the threshold itself is late
        (False, -30, None),
        (True, 10, None),  # already late: nothing new
        (True, 6, None),  # under the threshold but inside the hysteresis band
        (True, 5, None),  # 10 - 5 is still inside
        (True, 4, RUNNING_LATE_CLEARED),
    ],
)
def test_late_change(late: bool, delay_min: int, event: str | None) -> None:
    assert late_change(late, delay_min, 10) == event
