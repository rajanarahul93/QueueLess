"""CLIENT_IP_HEADER through the real limiter: separate buckets per client, no spoofing."""

import secrets
from collections.abc import AsyncIterator

import pytest

from tests.integration.api import Api, new_email, open_api

pytestmark = pytest.mark.anyio

LIMIT = 2


async def login(api: Api, header_value: str | None, **extra: str) -> int:
    headers = dict(extra) | ({"x-real-ip": header_value} if header_value else {})
    response = await api.client.post(
        "/v1/auth/login",
        json={"email": new_email(), "password": secrets.token_hex(12)},
        headers=headers,
    )
    return response.status_code


@pytest.fixture
async def behind_proxy(migrated_url: str, redis_url: str) -> AsyncIterator[Api]:
    overrides: dict[str, object] = {"client_ip_header": "x-real-ip", "login_ip_limit": LIMIT}
    async with open_api(migrated_url, redis_url, overrides) as api:
        yield api


@pytest.fixture
async def direct(migrated_url: str, redis_url: str) -> AsyncIterator[Api]:
    async with open_api(migrated_url, redis_url, {"login_ip_limit": LIMIT}) as api:
        yield api


async def test_each_client_behind_the_proxy_gets_its_own_bucket(behind_proxy: Api) -> None:
    for _ in range(LIMIT):
        assert await login(behind_proxy, "203.0.113.1") == 401
    assert await login(behind_proxy, "203.0.113.1") == 429  # that client is out
    assert await login(behind_proxy, "203.0.113.2") == 401  # another client is not


async def test_a_forged_forwarded_for_does_not_move_the_client(behind_proxy: Api) -> None:
    for _ in range(LIMIT):
        assert await login(behind_proxy, "203.0.113.1") == 401
    for guess in ("192.0.2.10", "192.0.2.11"):
        spoofed = await login(behind_proxy, "203.0.113.1", **{"x-forwarded-for": guess})
        assert spoofed == 429


async def test_an_invalid_header_value_falls_back_to_the_shared_peer_bucket(
    behind_proxy: Api,
) -> None:
    for _ in range(LIMIT):
        assert await login(behind_proxy, "not-an-ip") == 401
    assert await login(behind_proxy, "also-not-an-ip") == 429  # both counted against the peer


async def test_unset_ignores_the_header_so_everyone_shares_the_peer(direct: Api) -> None:
    for index in range(LIMIT):
        assert await login(direct, f"203.0.113.{index + 1}") == 401
    assert await login(direct, "203.0.113.200") == 429  # a new header value changes nothing
