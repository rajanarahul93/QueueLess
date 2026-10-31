"""Every kind of queue writer at once on ONE queue: all now take the queue's event lock just
before commit. No deadlock, no 500, and the queue's outbox ids follow commit order."""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from app.core.security import create_access_token
from app.models import OutboxEvent, Queue
from tests.concurrency.test_booking_race import customers
from tests.concurrency.test_join_race import Load, measured, report, tracked
from tests.integration.api import Api
from tests.integration.test_availability import utc
from tests.integration.test_booking import world
from tests.integration.test_queues import add_token, queue_id
from tests.integration.test_tokens import settings, token_row

pytestmark = pytest.mark.anyio


def owner_of(api: Api, user_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(api.settings, user_id, 'customer')}"}


async def test_mixed_writers_on_one_queue_never_deadlock_and_ids_follow_commit_order(
    api: Api, capsys: pytest.CaptureFixture[str]
) -> None:
    for run in range(3):
        w = await world(api)
        await settings(w, max_walk_ins=1000)
        queue = await queue_id(w)
        team = [w.owner, *[await w.staff() for _ in range(3)]]
        start = utc(8, day=1)
        waiting = [
            await add_token(api, w, queue, n, "waiting", 0, start + timedelta(seconds=n))
            for n in range(1, 31)
        ]
        called = [await add_token(api, w, queue, n, "called") for n in range(31, 41)]
        serving = [await add_token(api, w, queue, n, "in_service") for n in range(41, 51)]
        async with api.sessions() as session:  # real joins continue from here
            await session.execute(
                update(Queue).where(Queue.id == uuid.UUID(queue)).values(last_token_no=50)
            )
            await session.commit()
        joiners = await customers(api, 10)
        leavers = [owner_of(api, (await token_row(api, t)).customer_id) for t in waiting[25:]]
        client = api.client
        requests: list[Any] = [
            *(client.post(f"/v1/queues/{queue}/call-next", headers=team[i % 4]) for i in range(10)),
            *(client.post(f"/v1/queues/{queue}/tokens", json={}, headers=h) for h in joiners),
            *(
                client.post(
                    f"/v1/tokens/{t}/start",
                    json={"version": 1, "service_id": w.service_id},
                    headers=team[0],
                )
                for t in called[:5]
            ),
            *(
                client.post(f"/v1/tokens/{t}/skip", json={"version": 1}, headers=team[1])
                for t in called[5:]
            ),
            *(
                client.post(f"/v1/tokens/{t}/complete", json={"version": 1}, headers=team[2])
                for t in serving
            ),
            *(
                client.delete(f"/v1/tokens/{t}", headers=h)
                for t, h in zip(waiting[25:], leavers, strict=True)
            ),
            *(
                client.patch(
                    f"/v1/tokens/{t}/priority",
                    json={"priority": 2, "version": 1},
                    headers=team[3],
                )
                for t in waiting[20:25]
            ),
        ]
        load = Load()
        with measured(api, load) as states:
            await asyncio.gather(*(tracked(load, request) for request in requests))
        report(capsys, f"mixed writers on one queue run {run + 1}", api, load, states)
        assert states.counts["40P01"] == 0 and states.others() == 0
        assert set(load.statuses) <= {"200", "201", "204", "VERSION_CONFLICT", "INVALID_TRANSITION"}
        async with api.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(OutboxEvent)
                        .where(OutboxEvent.queue_id == uuid.UUID(queue))
                        .order_by(OutboxEvent.id)
                    )
                ).all()
            )
        versions: dict[str, list[int]] = {}
        for row in rows:
            versions.setdefault(row.payload["token_id"], []).append(row.payload["version"])
        assert all(v == sorted(v) and len(set(v)) == len(v) for v in versions.values())
        joined = [r.payload["token_no"] for r in rows if r.event_type == "token.joined"]
        assert joined == sorted(joined) and len(joined) == load.statuses["201"]
        expected_events = sum(
            n for code, n in load.statuses.items() if code in ("200", "201", "204")
        )
        assert len(rows) == expected_events  # exactly one event per successful change
