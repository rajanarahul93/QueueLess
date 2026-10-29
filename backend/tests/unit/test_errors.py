import logging

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from app.core.errors import DomainError, RateLimited, Unauthenticated, register_error_handlers

pytestmark = pytest.mark.anyio


class SlotTaken(DomainError):
    code = "SLOT_TAKEN"
    status = 409
    title = "Slot taken"


class Credentials(BaseModel):
    password: str


def make_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/slot")
    async def slot() -> None:
        raise SlotTaken("That slot was just booked", next_slots=["10:30", "11:00"])

    @app.get("/limited")
    async def limited() -> None:
        raise RateLimited(retry_after_sec=7)

    @app.get("/unauth")
    async def unauth() -> None:
        raise Unauthenticated()

    @app.post("/login")
    async def login(body: Credentials) -> None:
        return None

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("SELECT secret FROM users failed")

    return app


async def get(method: str, path: str, **kwargs: object) -> httpx.Response:
    # Starlette re-raises unhandled errors after the handler ran; the test wants the response.
    transport = httpx.ASGITransport(app=make_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)  # type: ignore[arg-type]


async def test_domain_error_is_a_problem_with_extras() -> None:
    response = await get("GET", "/slot")
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Slot taken",
        "status": 409,
        "code": "SLOT_TAKEN",
        "detail": "That slot was just booked",
        "next_slots": ["10:30", "11:00"],
    }


async def test_rate_limited_carries_retry_after() -> None:
    response = await get("GET", "/limited")
    assert (response.status_code, response.json()["code"]) == (429, "RATE_LIMITED")
    assert response.headers["retry-after"] == "7"


async def test_unauthenticated_default_detail() -> None:
    response = await get("GET", "/unauth")
    assert (response.status_code, response.json()["code"]) == (401, "UNAUTHENTICATED")
    assert response.json()["detail"] == "Unauthenticated"


async def test_validation_error_is_422_and_does_not_echo_input() -> None:
    response = await get("POST", "/login", json={"password": ["hunter2"]})
    body = response.json()
    assert (response.status_code, body["code"]) == (422, "VALIDATION_ERROR")
    assert body["errors"][0]["loc"] == ["body", "password"]
    assert "hunter2" not in response.text


async def test_unknown_route_and_wrong_method() -> None:
    missing = await get("GET", "/nope")
    assert (missing.status_code, missing.json()["code"]) == (404, "NOT_FOUND")
    wrong = await get("POST", "/slot")
    assert (wrong.status_code, wrong.json()["code"]) == (405, "METHOD_NOT_ALLOWED")
    assert wrong.headers["allow"] == "GET"
    assert wrong.headers["content-type"] == "application/problem+json"


async def test_unhandled_error_is_500_without_internals(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        response = await get("GET", "/boom", headers={"x-request-id": "req-1"})
    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert "SELECT" not in response.text and "RuntimeError" not in response.text
    record = next(r for r in caplog.records if "req-1" in r.getMessage())
    assert record.exc_info is not None
    assert "SELECT secret" in str(record.exc_info[1])  # the detail is logged, never returned
