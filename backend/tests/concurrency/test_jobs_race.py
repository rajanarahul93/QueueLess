"""Worker jobs racing staff actions, and the walk-in ratio under concurrent call-next."""

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from alembic import command
from sqlalchemy import select

from app.core.security import create_access_token
from app.models import QueueToken
from app.services import queue_jobs
from app.services.queue import call_order
from tests.concurrency.test_join_race import Load, measured, report, tracked
from tests.integration.api import Api
from tests.integration.db import alembic_config, reset_schema
from tests.integration.test_availability import utc
from tests.integration.test_booking import World, world
from tests.integration.test_jobs import past_queue, queue_status, ratio_queue
from tests.integration.test_queues import add_token, queue_id
from tests.integration.test_serving import no_shows
from tests.integration.test_tokens import token_row

pytestmark = pytest.mark.anyio

DUE = utc(10, day=1)  # an hour after the 09:00 calls below: past every grace period


@pytest.fixture
def migrated_url(postgres_url: str) -> str:
    """A fresh schema per test: the jobs scan every business. (Sync: Alembic runs its own loop.)"""
    reset_schema(postgres_url)
    command.upgrade(alembic_config(postgres_url), "head")
    return postgres_url


def headers_for(api: Api, user_id: uuid.UUID, role: str = "customer") -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(api.settings, user_id, role)}"}


def act(
    api: Api, headers: dict[str, str], token: str, action: str, version: int, **body: object
) -> Any:
    return api.client.post(
        f"/v1/tokens/{token}/{action}", json={"version": version, **body}, headers=headers
    )


async def staff_team(w: World) -> list[dict[str, str]]:
    return [w.owner, *[await w.staff() for _ in range(4)]]


def sources(tokens: list[Any]) -> str:
    return "".join("A" if t.source == "appointment" else "W" for t in tokens)


async def test_fifty_concurrent_call_next_keep_the_ratio_three_times(
    api: Api, capsys: pytest.CaptureFixture[str]
) -> None:
    for run in range(3):
        w = await world(api)
        spec = "AAAWAAWAAAAWAWAAAAWW" * 2 + "AAWAAWAAWW"  # 30 appointments, 20 walk-ins
        queue = await ratio_queue(w, spec)
        async with api.sessions() as session:
            initial = list(
                (
                    await session.scalars(
                        select(QueueToken)
                        .where(QueueToken.queue_id == uuid.UUID(queue))
                        .order_by(
                            QueueToken.priority.desc(), QueueToken.queued_at, QueueToken.token_no
                        )
                    )
                ).all()
            )
        expected = sources(call_order(initial, [], 1, 2))
        team = await staff_team(w)
        load = Load()
        started = time.perf_counter()
        with measured(api, load) as states:
            responses = await asyncio.gather(
                *(
                    tracked(
                        load, api.client.post(f"/v1/queues/{queue}/call-next", headers=team[i % 5])
                    )
                    for i in range(50)
                )
            )
        elapsed_ms = (time.perf_counter() - started) * 1000
        report(capsys, f"50 call-next, ratio 1:2, run {run + 1}", api, load, states)
        assert load.statuses == {"200": 50}
        assert len({r.json()["id"] for r in responses}) == 50  # nobody called twice
        async with api.sessions() as session:
            called = list(
                (
                    await session.scalars(
                        select(QueueToken)
                        .where(QueueToken.queue_id == uuid.UUID(queue))
                        .order_by(QueueToken.called_at)
                    )
                ).all()
            )
        actual = sources(called)
        with capsys.disabled():
            print(f"  burst {elapsed_ms:.0f} ms; call order {actual}")
        assert actual == expected  # the history never broke the ratio


async def called_due(w: World, number: int, **values: Any) -> str:
    return await add_token(
        w.api, w, await queue_id(w), number, "called", called_at=utc(9, day=1), **values
    )


@pytest.mark.parametrize("action", ["start", "skip", "leave"])
async def test_sweeper_races_a_staff_action_or_a_leave(
    api: Api, action: str, capsys: pytest.CaptureFixture[str]
) -> None:
    w = await world(api)
    outcomes = []
    for number in range(1, 6):
        token = await called_due(w, number)
        row = await token_row(api, token)
        if action == "leave":
            request = api.client.delete(
                f"/v1/tokens/{token}", headers=headers_for(api, row.customer_id)
            )
        else:
            extra = {"service_id": w.service_id} if action == "start" else {}
            request = act(api, w.owner, token, action, 1, **extra)
        response, summary = await asyncio.gather(
            request, queue_jobs.sweep_no_shows(api.sessions, DUE)
        )
        final = (await token_row(api, token)).status
        outcomes.append((response.status_code, summary.changed, final))
        assert response.status_code != 500 and summary.failed == 0
        swept = final == "no_show"
        assert swept == (summary.changed == 1) == (response.status_code == 409), outcomes[-1]
    with capsys.disabled():
        print(f"\nsweeper vs {action} (status, swept, final) x5: {outcomes}")


async def test_promotion_races_a_staff_priority_change(
    api: Api, capsys: pytest.CaptureFixture[str]
) -> None:
    w = await world(api)
    queue = await queue_id(w)
    outcomes = []
    for number in range(1, 6):
        joined = utc(9, day=1)
        token = await add_token(api, w, queue, number, "waiting", 0, joined, joined_at=joined)
        response, summary = await asyncio.gather(
            api.client.patch(
                f"/v1/tokens/{token}/priority", json={"priority": 2, "version": 1}, headers=w.owner
            ),
            queue_jobs.promote_waiting(api.sessions, DUE),
        )
        final = (await token_row(api, token)).priority
        outcomes.append((response.status_code, summary.changed, final))
        assert response.status_code != 500
        # Staff first: priority 2, promotion skipped. Promotion first: priority 1, staff 409.
        assert (final, response.status_code, summary.changed) in {(2, 200, 0), (1, 409, 1)}
    with capsys.disabled():
        print(f"\npromotion vs staff priority (status, promoted, final) x5: {outcomes}")


async def test_end_of_day_racing_complete_and_start(api: Api) -> None:
    w = await world(api)
    queue = await past_queue(w, datetime(2026, 10, 30, tzinfo=UTC).date())
    serving = await add_token(api, w, queue, 1, "in_service")
    called = await add_token(api, w, queue, 2, "called", called_at=utc(9, day=1))
    now = utc(10, day=1)
    completed, started, summary = await asyncio.gather(
        act(api, w.owner, serving, "complete", 1),
        act(api, w.owner, called, "start", 1, service_id=w.service_id),
        queue_jobs.end_of_day(api.sessions, now),
    )
    assert completed.status_code == 200  # end of day never touches an in-service token
    final = (await token_row(api, called)).status
    assert (final == "in_service") == (started.status_code == 200)
    assert (final == "cancelled") == (started.status_code == 409)
    await queue_jobs.end_of_day(api.sessions, now)
    assert await queue_status(api, queue) == "closed"
    assert summary.failed == 0


async def test_two_sweeper_runs_at_once_count_every_no_show_once(
    api: Api, capsys: pytest.CaptureFixture[str]
) -> None:
    person = await api.create_user("customer", phone=f"+91{uuid.uuid4().int % 10**10:010d}")
    tokens = []
    for _ in range(5):  # one customer, one active token per queue: five businesses
        w = await world(api)
        tokens.append(await called_due(w, 1, customer_id=person.id))
        tokens.append(await called_due(w, 2))
    first, second = await asyncio.gather(
        queue_jobs.sweep_no_shows(api.sessions, DUE), queue_jobs.sweep_no_shows(api.sessions, DUE)
    )
    with capsys.disabled():
        print(f"\ntwo sweeper runs: {vars(first)} | {vars(second)}")
    assert first.changed + second.changed == 10
    assert first.failed == second.failed == 0
    assert {(await token_row(api, t)).status for t in tokens} == {"no_show"}
    assert await no_shows(api, person.id) == 5
