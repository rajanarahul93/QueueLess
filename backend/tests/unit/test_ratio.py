from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services.queue import call_order, walk_in_turn

A, W = "appointment", "walk_in"


@pytest.mark.parametrize(
    ("history", "walk_ins", "appointments", "turn"),
    [
        ([], 1, 2, False),  # no history yet: base order
        ([A], 1, 2, False),
        ([A, A], 1, 2, True),  # 1:2, two appointments in a row
        ([W, A, A], 1, 2, False),  # the walk-in was just served
        ([A, W, A], 1, 2, False),
        ([A, A, A], 2, 3, True),  # 2:3
        ([W, A, A, A], 2, 3, True),  # one of two walk-ins served so far
        ([W, W, A, A, A], 2, 3, False),  # both walk-ins served
        ([A, A], 2, 3, False),
        ([A], 1, 1, True),  # 1:1 alternates
        ([W], 1, 1, False),
        ([W, A], 1, 1, False),
    ],
)
def test_walk_in_turn(history: list[str], walk_ins: int, appointments: int, turn: bool) -> None:
    assert walk_in_turn(history, walk_ins, appointments) is turn


@dataclass
class Token:
    source: str
    priority: int
    queued_at: datetime
    token_no: int


def tokens(spec: str) -> list[Any]:
    """'AAW' -> appointment, appointment, walk-in, queued in that order (the base order)."""
    start = datetime(2026, 11, 2, 9, tzinfo=UTC)
    return [
        Token(A if c == "A" else W, 2 if c == "P" else 0, start + timedelta(minutes=i), i + 1)
        for i, c in enumerate(spec)
    ]


def sources(order: list[Any]) -> str:
    return "".join("P" if t.priority == 2 else ("A" if t.source == A else "W") for t in order)


@pytest.mark.parametrize(
    ("spec", "walk_ins", "appointments", "expected"),
    [
        ("AAAAWW", 1, 2, "AAWAAW"),  # 1:2
        ("AAAAAAWWWW", 2, 3, "AAAWWAAAWW"),  # 2:3
        ("AAAWWW", 1, 1, "AWAWAW"),  # 1:1
        ("AAAA", 1, 2, "AAAA"),  # no walk-in waiting: appointments continue
        ("WWW", 1, 2, "WWW"),
    ],
)
def test_call_order_follows_the_ratio(
    spec: str, walk_ins: int, appointments: int, expected: str
) -> None:
    assert sources(call_order(tokens(spec), [], walk_ins, appointments)) == expected


def test_a_waiting_priority_two_token_suspends_the_walk_in_turn() -> None:
    queue = tokens("AAWP")  # P is a priority-2 walk-in
    queue = [queue[3], *queue[:3]]  # base order puts priority 2 first
    # P goes first although it is a walk-in's turn; being a walk-in, it also fills that turn.
    assert sources(call_order(queue, [A, A], 1, 2)) == "PAAW"
