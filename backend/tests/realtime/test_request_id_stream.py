"""x-request-id on real server-sent-event responses, served by uvicorn like in production."""

import uuid

import httpx
import pytest

from tests.integration.api import Api
from tests.realtime.conftest import open_stream, relay, serve
from tests.realtime.test_streams import setup, staff_token

pytestmark = pytest.mark.anyio


def is_generated(value: str) -> bool:
    return uuid.UUID(value).version == 4


async def test_a_live_stream_response_carries_a_request_id(api: Api) -> None:
    w, queue = await setup(api)
    token = await staff_token(api, w.owner, queue)
    async with (
        relay(api),
        serve(api) as (base, _),
        httpx.AsyncClient(base_url=base, timeout=30) as http,
    ):
        async with open_stream(http, f"/v1/queues/{queue}/stream?stream_token={token}") as stream:
            assert stream.response.headers["content-type"].startswith("text/event-stream")
            assert is_generated(stream.response.headers["x-request-id"])
            await stream.expect("snapshot")  # the stream itself still works with the middleware


async def test_a_stream_keeps_a_safe_inbound_id(api: Api) -> None:
    w, queue = await setup(api)
    token = await staff_token(api, w.owner, queue)
    async with (
        relay(api),
        serve(api) as (base, _),
        httpx.AsyncClient(base_url=base, timeout=30) as http,
    ):
        async with open_stream(
            http, f"/v1/queues/{queue}/stream?stream_token={token}", **{"x-request-id": "edge-42"}
        ) as stream:
            assert stream.response.headers["x-request-id"] == "edge-42"


async def test_a_refused_stream_carries_one_too(api: Api) -> None:
    async with serve(api) as (base, _), httpx.AsyncClient(base_url=base) as http:
        response = await http.get(f"/v1/queues/{uuid.uuid4()}/stream?stream_token=bad")
    assert response.status_code == 401
    assert is_generated(response.headers["x-request-id"])
