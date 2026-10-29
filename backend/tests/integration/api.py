import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.main import create_app
from app.models import User
from app.services.notifications import MockSmsProvider
from tests.helpers import make_settings

ORIGIN = "http://web.test"


class MutableClock:
    """Starts at the real time (JWT checks use the real clock) and only moves when told to."""

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


@dataclass
class Api:
    app: FastAPI
    client: httpx.AsyncClient  # shares one cookie jar across its requests
    settings: Settings
    clock: MutableClock

    @property
    def sms(self) -> MockSmsProvider:
        assert isinstance(self.app.state.sms, MockSmsProvider)
        return self.app.state.sms

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        sessions: async_sessionmaker[AsyncSession] = self.app.state.session_factory
        return sessions

    @property
    def redis(self) -> Redis:
        redis: Redis = self.app.state.redis
        return redis

    def new_client(self) -> httpx.AsyncClient:
        """A second device: its own cookie jar."""
        return httpx.AsyncClient(transport=self.client._transport, base_url="https://test")

    async def request_otp(self, phone: str) -> httpx.Response:
        return await self.client.post("/v1/auth/otp/request", json={"phone": phone})

    async def verify_otp(
        self,
        phone: str,
        code: str,
        name: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> httpx.Response:
        body = {"phone": phone, "code": code} | ({"name": name} if name else {})
        return await (client or self.client).post("/v1/auth/otp/verify", json=body)

    def last_code(self) -> str:
        return self.sms.sent[-1][1]

    async def create_user(self, role: str, **values: object) -> User:
        defaults: dict[str, object] = {"name": "n", "role": role}
        async with self.sessions() as session:
            user = (
                await session.execute(insert(User).values(defaults | values).returning(User))
            ).scalar_one()
            await session.commit()
            return user


@asynccontextmanager
async def open_api(
    database_url: str,
    redis_url: str,
    overrides: dict[str, object] | None = None,
    *,
    flush_redis: bool = True,
) -> AsyncIterator[Api]:
    clock = MutableClock()
    settings = make_settings(
        database_url=database_url, redis_url=redis_url, cors_origins=[ORIGIN], **(overrides or {})
    )
    app = create_app(settings, clock)
    async with app.router.lifespan_context(app):
        if flush_redis:  # per-IP limits would otherwise leak between tests
            await app.state.redis.flushdb()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
            yield Api(app, client, settings, clock)


def new_phone() -> str:
    return f"+91{uuid.uuid4().int % 10**10:010d}"


def new_email() -> str:
    return f"{uuid.uuid4().hex[:12]}@example.test"
