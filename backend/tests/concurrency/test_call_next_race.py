"""Staff actions under concurrency (NFR-4, FR-29) through the real HTTP app. Any 500 fails."""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update

from app.core.security import create_access_token
from app.models import QueueToken, ServiceSession, User
from tests.concurrency.test_join_race import Load, measured, report, tracked
from tests.integration.api import Api
from tests.integration.test_availability import Headers, utc
from tests.integration.test_booking import World, world
from tests.integration.test_queues import add_token, queue_id
from tests.integration.test_serving import appointment_status, appointment_token, no_shows

pytestmark = pytest.mark.anyio


def call(api: Api, headers: Headers, queue: str) -> Any:
    return api.client.post(f"/v1/queues/{queue}/call-next", headers=headers)


def act(api: Api, headers: Headers, token: str, action: str, version: int, **body: object) -> Any:
    return api.client.post(
        f"/v1/tokens/{token}/{action}", json={"version": version, **body}, headers=headers
    )


def leave(api: Api, headers: Headers, token: str) -> Any:
    return api.client.delete(f"/v1/tokens/{token}", headers=headers)


async def staff_team(w: World) -> list[Headers]:
    """The owner plus four staff accounts."""
    return [w.owner, *[await w.staff() for _ in range(4)]]


async def waiting_tokens(api: Api, w: World, queue: str, count: int) -> list[str]:
    """`count` waiting tokens with distinct queued_at, returned in base order."""
    return [
        await add_token(api, w, queue, no, "waiting", 0, utc(3) + timedelta(seconds=no))
        for no in range(1, count + 1)
    ]


async def token(api: Api, token_id: str) -> QueueToken:
    async with api.sessions() as session:
        found = await session.get(QueueToken, uuid.UUID(token_id))
        assert found is not None
        return found


def customer_headers(api: Api, user_id: uuid.UUID) -> Headers:
    return {"Authorization": f"Bearer {create_access_token(api.settings, user_id, 'customer')}"}


async def test_fifty_staff_calls_take_fifty_distinct_tokens_three_times(
    api: Api, capsys: pytest.CaptureFixture[str]
) -> None:
    for run in range(3):
        w = await world(api)
        queue = await queue_id(w)
        team = await staff_team(w)
        expected = await waiting_tokens(api, w, queue, 50)
        load = Load()
        with measured(api, load) as states:
            responses = await asyncio.gather(
                *(tracked(load, call(api, team[i % len(team)], queue)) for i in range(50))
            )
        report(capsys, f"50 call-next on 50 tokens run {run + 1}", api, load, states)
        assert load.statuses == {"200": 50}
        called = [r.json()["id"] for r in responses]
        assert len(set(called)) == 50 and set(called) == set(expected)
        for token_id in expected:
            row = await token(api, token_id)
            assert (row.status, row.version) == ("called", 2)


async def test_sixty_calls_on_fifty_tokens(api: Api, capsys: pytest.CaptureFixture[str]) -> None:
    w = await world(api)
    queue = await queue_id(w)
    team = await staff_team(w)
    await waiting_tokens(api, w, queue, 50)
    load = Load()
    with measured(api, load) as states:
        await asyncio.gather(*(tracked(load, call(api, team[i % 5], queue)) for i in range(60)))
    report(capsys, "60 call-next on 50 tokens", api, load, states)
    assert load.statuses == {"200": 50, "204": 10}


async def test_two_staff_one_waiting_token(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    team = await staff_team(w)
    (only,) = await waiting_tokens(api, w, queue, 1)
    load = Load()
    responses = await asyncio.gather(*(tracked(load, call(api, h, queue)) for h in team[:2]))
    assert load.statuses == {"200": 1, "204": 1}
    assert [r.json()["id"] for r in responses if r.status_code == 200] == [only]


async def test_call_next_racing_a_customer_leave(
    api: Api, capsys: pytest.CaptureFixture[str]
) -> None:
    outcomes: list[tuple[int, int]] = []
    for _ in range(5):
        w = await world(api)
        queue = await queue_id(w)
        first, second = await waiting_tokens(api, w, queue, 2)
        owner_of_first = customer_headers(api, (await token(api, first)).customer_id)
        load = Load()
        called, left = await asyncio.gather(
            tracked(load, call(api, w.owner, queue)),
            tracked(load, leave(api, owner_of_first, first)),
        )
        outcomes.append((called.status_code, left.status_code))
        row = await token(api, first)
        if called.json()["id"] == first:  # called first, then the customer left (or lost)
            assert row.called_at is not None
            assert row.status in {"called", "cancelled"}
            assert left.status_code in {204, 409}
        else:  # the leave came first: the call went to the next token
            assert called.json()["id"] == second
            assert (row.status, row.called_at, left.status_code) == ("cancelled", None, 204)
    with capsys.disabled():
        print(f"\ncall-next vs leave (call status, leave status) x5: {outcomes}")


async def test_two_starts_and_two_completes(api: Api) -> None:
    w = await world(api)
    queue = await queue_id(w)
    team = await staff_team(w)
    called = await add_token(api, w, queue, 1, "called")
    load = Load()
    await asyncio.gather(
        *(tracked(load, act(api, h, called, "start", 1, service_id=w.service_id)) for h in team[:2])
    )
    assert load.statuses == {"200": 1, "VERSION_CONFLICT": 1}
    async with api.sessions() as session:
        sessions = await session.scalar(
            select(func.count())
            .select_from(ServiceSession)
            .where(ServiceSession.token_id == uuid.UUID(called))
        )
    assert sessions == 1
    load = Load()
    await asyncio.gather(*(tracked(load, act(api, h, called, "complete", 2)) for h in team[:2]))
    assert load.statuses == {"200": 1, "VERSION_CONFLICT": 1}


async def test_start_racing_a_leave(api: Api, capsys: pytest.CaptureFixture[str]) -> None:
    outcomes = []
    w = await world(api)
    queue = await queue_id(w)
    for no in range(1, 6):
        called = await add_token(api, w, queue, no, "called")
        owner_of_it = customer_headers(api, (await token(api, called)).customer_id)
        started, left = await asyncio.gather(
            act(api, w.owner, called, "start", 1, service_id=w.service_id),
            leave(api, owner_of_it, called),
        )
        outcomes.append((started.status_code, left.status_code))
        assert 500 not in outcomes[-1]
        assert (started.status_code == 200) != (left.status_code == 204), outcomes[-1]
        final = (await token(api, called)).status
        assert final == ("in_service" if started.status_code == 200 else "cancelled")
    with capsys.disabled():
        print(f"\nstart vs leave (start status, leave status) x5: {outcomes}")


async def third_skip_ready(w: World) -> tuple[User, str, str]:
    """An appointment token, called, already skipped twice."""
    user, appointment_id, token_id = await appointment_token(w)
    async with w.api.sessions() as session:
        await session.execute(
            update(QueueToken)
            .where(QueueToken.id == uuid.UUID(token_id))
            .values(status="called", skip_count=2)
        )
        await session.commit()
    return user, appointment_id, token_id


@pytest.mark.parametrize("rival", ["skip", "no-show"])
async def test_third_skip_races_exactly_one_no_show(api: Api, rival: str) -> None:
    w = await world(api)
    team = await staff_team(w)
    user, appointment_id, token_id = await third_skip_ready(w)
    load = Load()
    await asyncio.gather(
        tracked(load, act(api, team[0], token_id, "skip", 1)),
        tracked(load, act(api, team[1], token_id, rival, 1)),
    )
    assert load.statuses == {"200": 1, "VERSION_CONFLICT": 1}
    assert (await token(api, token_id)).status == "no_show"
    assert await no_shows(api, user.id) == 1
    assert await appointment_status(api, appointment_id) == "no_show"


async def test_no_shows_in_many_queues_count_exactly(
    api: Api, capsys: pytest.CaptureFixture[str]
) -> None:
    person = await api.create_user("customer", phone=f"+91{uuid.uuid4().int % 10**10:010d}")
    jobs = []
    for _ in range(5):  # one active token per queue, so five businesses
        w = await world(api)
        queue = await queue_id(w)
        token_id = await add_token(api, w, queue, 1, "called", customer_id=person.id)
        jobs.append(act(api, w.owner, token_id, "no-show", 1))
    load = Load()
    with measured(api, load) as states:
        await asyncio.gather(*(tracked(load, job) for job in jobs))
    report(capsys, "5 no-shows, one customer, 5 queues", api, load, states)
    assert load.statuses == {"200": 5}
    assert await no_shows(api, person.id) == 5
