"""Token state machine (FR-11, FR-12). Pure: no database, no clock; `now` is passed in.

Role-free: who may trigger an event is decided by the services that call this. Bumping `version`
is also the caller's job, in the same UPDATE that applies `values`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal

from app.core.errors import InvalidTransition

TokenStatus = Literal["waiting", "called", "in_service", "completed", "no_show", "cancelled"]
Event = Literal["call", "start", "complete", "skip", "mark_no_show", "cancel"]

STATUSES: tuple[TokenStatus, ...] = (
    "waiting",
    "called",
    "in_service",
    "completed",
    "no_show",
    "cancelled",
)
EVENTS: tuple[Event, ...] = ("call", "start", "complete", "skip", "mark_no_show", "cancel")
TERMINAL: frozenset[TokenStatus] = frozenset({"completed", "no_show", "cancelled"})
MAX_SKIPS = 2  # the third skip makes the token a no-show

Value = datetime | int | str | None


@dataclass(frozen=True)
class Transition:
    status: TokenStatus
    values: Mapping[str, Value]  # every column to update, `status` included; read-only


def _result(status: TokenStatus, **changes: Value) -> Transition:
    return Transition(status, MappingProxyType({"status": status, **changes}))


def transition(status: str, event: str, *, skip_count: int, now: datetime) -> Transition:
    """The next state and the columns that change, or InvalidTransition for an illegal pair."""
    match (status, event):
        case ("waiting", "call"):
            return _result("called", called_at=now)
        case ("waiting", "cancel") | ("called", "cancel"):
            return _result("cancelled")
        case ("called", "start"):
            return _result("in_service", started_at=now)
        case ("called", "skip") if skip_count >= MAX_SKIPS:
            return _result("no_show", skip_count=skip_count + 1)
        case ("called", "skip"):
            # Back of its priority tier; joined_at and priority never change here.
            return _result("waiting", skip_count=skip_count + 1, queued_at=now, called_at=None)
        case ("called", "mark_no_show"):
            return _result("no_show")
        case ("in_service", "complete"):
            return _result("completed", completed_at=now)
    raise InvalidTransition(f"Cannot {event} a token that is {status}")
