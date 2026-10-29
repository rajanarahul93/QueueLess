import httpx
import pytest

from app.main import create_app
from tests.helpers import make_settings


@pytest.mark.anyio
async def test_healthz() -> None:
    transport = httpx.ASGITransport(app=create_app(make_settings()))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
