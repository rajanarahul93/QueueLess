"""The staff stream carries each token's service; the customer stream still carries none."""

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import insert, select

from app.models import OutboxEvent, Service
from tests.integration.api import Api
from tests.integration.test_serving import act, call_next
from tests.integration.test_tokens import join
from tests.realtime.conftest import open_stream, relay, serve
from tests.realtime.test_streams import joined, setup, staff_token

pytestmark = pytest.mark.anyio

SERVICE_KEYS = {"service_id", "service_name"}


def keys_at_any_depth(node: Any, found: list[str] | None = None) -> list[str]:
    collected = [] if found is None else found
    if isinstance(node, dict):
        for key, value in node.items():
            collected.append(key)
            keys_at_any_depth(value, collected)
    elif isinstance(node, list):
        for item in node:
            keys_at_any_depth(item, collected)
    return collected


async def test_the_staff_snapshot_and_the_started_delta_carry_the_service(api: Api) -> None:
    w, queue = await setup(api)
    staff = await w.staff()
    _, customer_token = await joined(api, queue)
    async with api.sessions() as session:
        name = await session.scalar(
            select(Service.name).where(Service.id == uuid.UUID(w.service_id))
        )
    stream = await staff_token(api, w.owner, queue)
    async with (
        relay(api),
        serve(api) as (base, _),
        httpx.AsyncClient(base_url=base, timeout=30) as http,
    ):
        async with open_stream(http, f"/v1/queues/{queue}/stream?stream_token={stream}") as s:
            snapshot = await s.expect("snapshot")
            [token] = snapshot["data"]["tokens"]
            # A walk-in before Start: the service is unknown, and said so rather than omitted.
            assert token["service_id"] is None and token["service_name"] is None
            called = (await call_next(api, staff, queue)).json()
            call_delta = (await s.expect("token.called"))["data"]["data"]
            assert call_delta["service_id"] is None and call_delta["service_name"] is None
            started = await act(
                api,
                staff,
                customer_token["id"],
                "start",
                called["version"],
                service_id=w.service_id,
            )
            assert started.status_code == 200, started.text
            delta = (await s.expect("token.started"))["data"]["data"]
            assert delta["service_id"] == w.service_id and delta["service_name"] == name
            assert delta["token_id"] == customer_token["id"]


async def test_an_old_event_without_the_fields_is_delivered_unchanged(api: Api) -> None:
    """Rows written before this change have no service keys; a replay must still work."""
    w, queue = await setup(api)
    _, customer_token = await joined(api, queue)
    stream = await staff_token(api, w.owner, queue)
    old_payload = {  # exactly what emit_token_event wrote before the service fields existed
        "token_id": customer_token["id"],
        "token_no": customer_token["token_no"],
        "status": "waiting",
        "priority": 0,
        "version": 1,
        "source": "walk_in",
        "skip_count": 0,
        "joined_at": None,
        "queued_at": None,
        "called_at": None,
        "started_at": None,
        "completed_at": None,
    }
    async with (
        relay(api),
        serve(api) as (base, _),
        httpx.AsyncClient(base_url=base, timeout=30) as http,
    ):
        async with open_stream(http, f"/v1/queues/{queue}/stream?stream_token={stream}") as s:
            await s.expect("snapshot")
            async with api.sessions() as session:
                await session.execute(
                    insert(OutboxEvent).values(
                        business_id=w.business_id,
                        queue_id=uuid.UUID(queue),
                        event_type="token.priority_changed",
                        payload=old_payload,
                    )
                )
                await session.commit()
            delta = (await s.expect("token.priority_changed"))["data"]["data"]
            assert not SERVICE_KEYS & set(delta)  # absent, which clients read as null
            assert delta["token_id"] == customer_token["id"]
            # The stream is still healthy: the next event arrives as usual.
            second = await join(api, (await joined(api, queue))[0], queue)
            assert second.status_code in (201, 409), second.text
            assert (await s.expect("token.joined"))["data"]["data"]["service_id"] is None


async def test_customer_frames_carry_no_service_keys(api: Api) -> None:
    w, queue = await setup(api)
    staff = await w.staff()
    headers, me = await joined(api, queue)
    async with (
        relay(api),
        serve(api) as (base, _),
        httpx.AsyncClient(base_url=base, timeout=30) as http,
    ):
        url = f"/v1/tokens/{me['id']}/stream?stream_token={me['stream_token']}"
        async with open_stream(http, url) as s:
            snapshot = await s.expect("snapshot")
            assert not SERVICE_KEYS & set(keys_at_any_depth(snapshot["data"]))
            called = (await call_next(api, staff, queue)).json()
            await act(api, staff, me["id"], "start", called["version"], service_id=w.service_id)
            update = await s.expect("token.updated")
            # The service staff just chose must not reach the customer's frames.
            assert not SERVICE_KEYS & set(keys_at_any_depth(update["data"]))
            assert w.service_id not in s.raw
    del headers
