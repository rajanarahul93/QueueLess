import httpx
import pytest
from sqlalchemy import text

from app.main import create_app
from tests.helpers import make_settings

pytestmark = pytest.mark.anyio


async def test_lifespan_creates_and_disposes_real_connections(
    migrated_url: str, redis_url: str
) -> None:
    app = create_app(make_settings(database_url=migrated_url, redis_url=redis_url))
    async with app.router.lifespan_context(app):
        async with app.state.session_factory() as session:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
        assert await app.state.redis.ping()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/healthz")).json() == {"status": "ok"}
    # Shutdown closed the Redis connection pool and disposed the engine.
    assert app.state.engine.pool.checkedout() == 0
