"""x-request-id: on every response, a strict inbound pattern, and in the log records."""

import logging
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.errors import register_error_handlers
from app.core.request_id import (
    REQUEST_ID_HEADER,
    RequestIdFilter,
    RequestIdFormatter,
    RequestIdMiddleware,
    accept_request_id,
)

pytestmark = pytest.mark.anyio


class Body(BaseModel):
    count: int


def make_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.add_middleware(RequestIdMiddleware)
    log = logging.getLogger("app.request_id_test")

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        log.warning("inside the request")
        return {"status": "ok"}

    @app.post("/typed")
    async def typed(body: Body) -> None:
        return None

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("an unexpected failure")

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            yield "retry: 1000\n\n"
            yield ": heartbeat\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=make_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def generated(value: str) -> bool:
    try:
        return uuid.UUID(value).version == 4 and str(uuid.UUID(value)) == value
    except ValueError:
        return False


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [("GET", "/ok", 200), ("GET", "/nowhere", 404), ("POST", "/typed", 422), ("GET", "/boom", 500)],
)
async def test_every_response_has_one(
    client: httpx.AsyncClient, method: str, path: str, status: int
) -> None:
    response = await client.request(
        method, path, json={"count": "many"} if method == "POST" else None
    )
    assert response.status_code == status
    assert generated(response.headers[REQUEST_ID_HEADER])
    assert len(response.headers.get_list(REQUEST_ID_HEADER)) == 1


async def test_a_stream_response_has_one(client: httpx.AsyncClient) -> None:
    async with client.stream("GET", "/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert generated(response.headers[REQUEST_ID_HEADER])
        assert ": heartbeat" in "".join([line async for line in response.aiter_lines()])


async def test_each_request_gets_its_own_value(client: httpx.AsyncClient) -> None:
    ids = {(await client.get("/ok")).headers[REQUEST_ID_HEADER] for _ in range(5)}
    assert len(ids) == 5


@pytest.mark.parametrize(
    "value", ["abc-123", "A.b_c-9", "x" * 64, "0f8fad5b-d9cb-469f-a165-70867728950e"]
)
async def test_a_safe_inbound_value_is_kept(client: httpx.AsyncClient, value: str) -> None:
    response = await client.get("/ok", headers={REQUEST_ID_HEADER: value})
    assert response.headers[REQUEST_ID_HEADER] == value


@pytest.mark.parametrize(
    "value", ["", "x" * 65, "has space", "semi;colon", "<script>", "a/b", "a,b", "a:b", "a=b"]
)
async def test_an_unsafe_inbound_value_is_replaced(client: httpx.AsyncClient, value: str) -> None:
    response = await client.get("/ok", headers={REQUEST_ID_HEADER: value})
    sent = response.headers[REQUEST_ID_HEADER]
    assert sent != value and generated(sent)


@pytest.mark.parametrize(
    "value",
    [None, "", "x" * 65, "line\nbreak", "tab\there", "\u00e9clair", "a b", "a\x00b", "\u2028"],
)
def test_the_pattern_refuses_what_a_header_could_smuggle(value: str | None) -> None:
    assert generated(accept_request_id(value))


@pytest.mark.parametrize("value", ["a", "A.b_c-9", "x" * 64])
def test_the_pattern_keeps_safe_values(value: str) -> None:
    assert accept_request_id(value) == value


async def test_the_value_reaches_the_log_records(client: httpx.AsyncClient) -> None:
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Collect()
    handler.addFilter(RequestIdFilter())
    logger = logging.getLogger("app.request_id_test")
    logger.addHandler(handler)
    try:
        response = await client.get("/ok", headers={REQUEST_ID_HEADER: "trace-me-1"})
    finally:
        logger.removeHandler(handler)
    assert response.headers[REQUEST_ID_HEADER] == "trace-me-1"
    assert [record.request_id for record in records] == ["trace-me-1"]  # type: ignore[attr-defined]


def test_outside_a_request_the_log_field_is_a_dash() -> None:
    record = logging.LogRecord("app.x", logging.INFO, "f.py", 1, "outside", None, None)
    assert RequestIdFilter().filter(record) is True
    assert record.request_id == "-"  # type: ignore[attr-defined]


async def test_the_unhandled_error_log_carries_the_same_id(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)
    response = await client.get("/boom", headers={REQUEST_ID_HEADER: "boom-77"})
    assert response.headers[REQUEST_ID_HEADER] == "boom-77"
    assert "request_id=boom-77" in caplog.text


def formatted(request_id: str, message: str, exc_info: bool = False) -> str:
    record = logging.LogRecord("app.x", logging.ERROR, "f.py", 1, message, None, None)
    record.request_id = request_id
    if exc_info:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            import sys

            record.exc_info = sys.exc_info()
    return RequestIdFormatter("%(levelname)s %(name)s %(message)s").format(record)


def test_a_line_inside_a_request_ends_with_its_id() -> None:
    assert formatted("abc-1", "hello") == "ERROR app.x hello request_id=abc-1"


def test_a_line_outside_a_request_is_untouched() -> None:
    assert formatted("-", "hello") == "ERROR app.x hello"


def test_a_line_that_already_names_the_id_is_not_doubled() -> None:
    assert formatted("abc-1", "unhandled error request_id=abc-1 GET /x") == (
        "ERROR app.x unhandled error request_id=abc-1 GET /x"
    )


def test_the_id_goes_on_the_first_line_of_a_traceback() -> None:
    lines = formatted("abc-1", "failed", exc_info=True).split("\n")
    assert lines[0] == "ERROR app.x failed request_id=abc-1"
    assert lines[-1] == "RuntimeError: boom"
