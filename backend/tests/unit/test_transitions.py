from datetime import UTC, datetime

import pytest

from app.core.errors import InvalidTransition
from app.services.transitions import EVENTS, STATUSES, transition

NOW = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)

# Written by hand from the product rules, NOT derived from the implementation: every legal
# (status, event) pair with the exact columns it sets, for a token that has never been skipped.
LEGAL: dict[tuple[str, str], dict[str, object]] = {
    ("waiting", "call"): {"status": "called", "called_at": NOW},
    ("waiting", "cancel"): {"status": "cancelled"},
    ("called", "start"): {"status": "in_service", "started_at": NOW},
    ("called", "skip"): {"status": "waiting", "skip_count": 1, "queued_at": NOW, "called_at": None},
    ("called", "mark_no_show"): {"status": "no_show"},
    ("called", "cancel"): {"status": "cancelled"},
    ("in_service", "complete"): {"status": "completed", "completed_at": NOW},
}
ALL_STATUSES = ("waiting", "called", "in_service", "completed", "no_show", "cancelled")
ALL_EVENTS = ("call", "start", "complete", "skip", "mark_no_show", "cancel")


def test_the_module_lists_exactly_the_six_statuses_and_six_events() -> None:
    assert set(STATUSES) == set(ALL_STATUSES) and set(EVENTS) == set(ALL_EVENTS)


@pytest.mark.parametrize("status", ALL_STATUSES)
@pytest.mark.parametrize("event", ALL_EVENTS)
def test_every_pair(status: str, event: str) -> None:
    expected = LEGAL.get((status, event))
    if expected is None:
        with pytest.raises(InvalidTransition) as error:
            transition(status, event, skip_count=0, now=NOW)
        assert error.value.code == "INVALID_TRANSITION" and error.value.status == 409
    else:
        result = transition(status, event, skip_count=0, now=NOW)
        assert result.status == expected["status"]
        assert dict(result.values) == expected


def test_seven_legal_pairs_out_of_thirty_six() -> None:
    legal = 0
    for status in ALL_STATUSES:
        for event in ALL_EVENTS:
            try:
                transition(status, event, skip_count=0, now=NOW)
                legal += 1
            except InvalidTransition:
                pass
    assert legal == len(LEGAL) == 7


@pytest.mark.parametrize(
    ("skip_count", "expected"),
    [
        (0, {"status": "waiting", "skip_count": 1, "queued_at": NOW, "called_at": None}),
        (1, {"status": "waiting", "skip_count": 2, "queued_at": NOW, "called_at": None}),
        (2, {"status": "no_show", "skip_count": 3}),  # the third skip
    ],
)
def test_skip_counts(skip_count: int, expected: dict[str, object]) -> None:
    assert dict(transition("called", "skip", skip_count=skip_count, now=NOW).values) == expected


@pytest.mark.parametrize("status", ["completed", "no_show", "cancelled"])
def test_terminal_states_have_no_exit(status: str) -> None:
    for event in ALL_EVENTS:
        for skips in (0, 2):
            with pytest.raises(InvalidTransition):
                transition(status, event, skip_count=skips, now=NOW)


@pytest.mark.parametrize(
    ("status", "event"),
    [
        ("cancelled", "cancel"),
        ("called", "call"),
        ("completed", "complete"),
        ("in_service", "start"),
    ],
)
def test_repeating_an_event_is_illegal_here(status: str, event: str) -> None:
    with pytest.raises(InvalidTransition):
        transition(status, event, skip_count=0, now=NOW)


def test_skip_never_touches_joined_at_or_priority_and_values_are_read_only() -> None:
    result = transition("called", "skip", skip_count=0, now=NOW)
    assert "joined_at" not in result.values and "priority" not in result.values
    with pytest.raises(TypeError):
        result.values["status"] = "called"  # type: ignore[index]
