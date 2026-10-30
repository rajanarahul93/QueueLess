"""Lazy queue creation under load: 50 simultaneous first lookups create exactly one row."""

import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from app.models import Queue
from tests.concurrency.test_booking_race import RUNS, customers
from tests.integration.api import Api
from tests.integration.test_booking import world

pytestmark = pytest.mark.anyio

CONCURRENT = 50


async def test_fifty_first_lookups_create_one_queue_five_times(api: Api) -> None:
    for _ in range(RUNS):
        w = await world(api)  # a fresh business, so the first lookup really creates the row
        heads = await customers(api, CONCURRENT)
        responses = await asyncio.gather(
            *(api.client.get(f"/v1/businesses/{w.business_id}/queue", headers=h) for h in heads)
        )
        assert {r.status_code for r in responses} == {200}, [r.text for r in responses[:3]]
        assert len({r.json()["id"] for r in responses}) == 1
        async with api.sessions() as session:
            rows = await session.scalar(
                select(func.count()).select_from(Queue).where(Queue.business_id == w.business_id)
            )
        assert rows == 1
        assert uuid.UUID(responses[0].json()["id"])
