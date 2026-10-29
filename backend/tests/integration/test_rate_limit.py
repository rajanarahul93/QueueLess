import asyncio
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import Depends
from redis.asyncio import Redis

from app.core.errors import RateLimited, ServiceUnavailable
from app.core.rate_limit import RateLimiter, rate_limit
from app.main import create_app
from tests.helpers import make_settings

pytestmark = pytest.mark.anyio


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url)
    yield client
    await client.aclose()


@pytest.fixture
def scope() -> str:
    return f"test-{uuid.uuid4().hex}"  # unique keys, so tests never see each other's hits


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


async def test_blocks_after_the_limit_with_retry_after(redis: Redis, scope: str) -> None:
    clock = Clock()
    limiter = RateLimiter(redis, clock)
    for _ in range(3):
        await limiter.check(scope, "a", limit=3, window_sec=60)
        clock.now += 1
    with pytest.raises(RateLimited) as error:
        await limiter.check(scope, "a", limit=3, window_sec=60)
    # The oldest hit was 3 s ago, so the window frees up in 57 s.
    assert error.value.headers["Retry-After"] == "57"


async def test_window_slides(redis: Redis, scope: str) -> None:
    clock = Clock()
    limiter = RateLimiter(redis, clock)
    await limiter.check(scope, "a", limit=2, window_sec=10)  # t=0
    clock.now += 5
    await limiter.check(scope, "a", limit=2, window_sec=10)  # t=5
    with pytest.raises(RateLimited):
        await limiter.check(scope, "a", limit=2, window_sec=10)
    clock.now += 5  # t=10: the hit at t=0 has left the window, the one at t=5 has not
    await limiter.check(scope, "a", limit=2, window_sec=10)
    with pytest.raises(RateLimited):
        await limiter.check(scope, "a", limit=2, window_sec=10)


async def test_denied_requests_are_not_recorded(redis: Redis, scope: str) -> None:
    clock = Clock()
    limiter = RateLimiter(redis, clock)
    await limiter.check(scope, "a", limit=1, window_sec=10)
    for _ in range(5):
        clock.now += 1
        with pytest.raises(RateLimited):
            await limiter.check(scope, "a", limit=1, window_sec=10)
    clock.now += 5  # t=10 since the only recorded hit
    await limiter.check(scope, "a", limit=1, window_sec=10)


async def test_identifiers_and_scopes_are_independent(redis: Redis, scope: str) -> None:
    limiter = RateLimiter(redis)
    await limiter.check(scope, "a", limit=1, window_sec=60)
    await limiter.check(scope, "b", limit=1, window_sec=60)
    await limiter.check(f"{scope}-other", "a", limit=1, window_sec=60)


async def test_concurrent_checks_admit_exactly_the_limit(redis: Redis, scope: str) -> None:
    limiter = RateLimiter(redis)
    results = await asyncio.gather(
        *(limiter.check(scope, "a", limit=10, window_sec=60) for _ in range(50)),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 10
    assert sum(isinstance(result, RateLimited) for result in results) == 40


async def test_dependency_returns_429_problem_with_retry_after(redis_url: str, scope: str) -> None:
    app = create_app(make_settings(redis_url=redis_url))

    @app.get("/limited", dependencies=[Depends(rate_limit(scope, limit=2, window_sec=60))])
    async def limited() -> dict[str, str]:
        return {"ok": "yes"}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            statuses = [(await client.get("/limited")).status_code for _ in range(2)]
            denied = await client.get("/limited")
    assert statuses == [200, 200]
    assert denied.status_code == 429
    assert denied.headers["content-type"] == "application/problem+json"
    assert 0 < int(denied.headers["retry-after"]) <= 60
    assert denied.json()["code"] == "RATE_LIMITED"


async def test_redis_down_fails_closed_with_503(scope: str) -> None:
    unreachable = Redis.from_url("redis://localhost:1/0")  # nothing listens on port 1
    try:
        with pytest.raises(ServiceUnavailable):
            await RateLimiter(unreachable).check(scope, "a", limit=5, window_sec=60)
    finally:
        await unreachable.aclose()


async def test_dependency_returns_503_problem_when_redis_is_down(scope: str) -> None:
    app = create_app(make_settings())  # redis_url points at a closed port

    @app.get("/limited", dependencies=[Depends(rate_limit(scope, limit=2, window_sec=60))])
    async def limited() -> dict[str, str]:
        return {"ok": "yes"}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/limited")
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
