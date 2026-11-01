"""Two refreshes of one cookie at once, through the real app and the Postgres row lock.

With the grace window on, the second waits for the first, finds the cookie just rotated and is
given the same successor: both succeed, one successor row exists and the family stays alive.
"""

import asyncio

import pytest
from sqlalchemy import select

from app.core.security import hash_refresh_token
from app.models import RefreshToken
from tests.integration.api import Api
from tests.integration.test_auth_session import cookie_attributes, login_owner, replay

pytestmark = pytest.mark.anyio

RUNS = 5


async def test_two_parallel_refreshes_of_one_cookie_get_the_same_successor(api: Api) -> None:
    assert api.settings.refresh_reuse_grace_sec > 0
    for _ in range(RUNS):
        login = await login_owner(api)
        old = api.client.cookies["refresh_token"]
        async with api.new_client() as first, api.new_client() as second:
            responses = await asyncio.gather(
                first.post("/v1/auth/refresh", headers=replay(old)),
                second.post("/v1/auth/refresh", headers=replay(old)),
            )
        assert [response.status_code for response in responses] == [200, 200]
        [successor] = {cookie_attributes(response)["value"] for response in responses}
        async with api.sessions() as session:
            rows = (
                await session.execute(
                    select(RefreshToken).where(RefreshToken.user_id == login.json()["user"]["id"])
                )
            ).scalars()
            live = {row.token_hash: row.revoked_at is None for row in rows}
        assert live == {hash_refresh_token(old): False, hash_refresh_token(successor): True}
        async with api.new_client() as holder:
            again = await holder.post("/v1/auth/refresh", headers=replay(successor))
        assert again.status_code == 200
