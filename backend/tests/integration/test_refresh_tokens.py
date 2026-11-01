import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.errors import Unauthenticated
from app.core.security import hash_refresh_token
from app.db.session import create_engine, create_session_factory
from app.models import RefreshToken, User
from app.services.auth import REUSE_GRACE_MAX_STEPS, issue_refresh_token, rotate_refresh_token
from tests.helpers import make_settings

pytestmark = pytest.mark.anyio

settings = make_settings()


@pytest.fixture
async def engine(migrated_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(migrated_url)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
async def user_id(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sessions() as session:
        created = await session.execute(
            insert(User)
            .values(name="u", phone=f"+91{uuid.uuid4().int % 10**10:010d}", role="owner")
            .returning(User.id)
        )
        await session.commit()
        return created.scalar_one()


async def login(sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> str:
    async with sessions() as session:
        return await issue_refresh_token(session, settings, user_id)


async def rotate(sessions: async_sessionmaker[AsyncSession], raw: str) -> tuple[uuid.UUID, str]:
    async with sessions() as session:
        return await rotate_refresh_token(session, settings, raw)


async def family_rows(
    sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> list[RefreshToken]:
    async with sessions() as session:
        rows = await session.execute(select(RefreshToken).where(RefreshToken.user_id == user_id))
        return list(rows.scalars())


@pytest.fixture
def no_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grace window off: every reuse of a rotated token revokes, as before the window existed."""
    monkeypatch.setitem(globals(), "settings", make_settings(refresh_reuse_grace_sec=0))


async def test_issue_stores_only_the_hash(
    sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> None:
    raw = await login(sessions, user_id)
    [row] = await family_rows(sessions, user_id)
    assert row.token_hash == hash_refresh_token(raw) != raw
    assert row.revoked_at is None
    assert row.expires_at > datetime.now(UTC) + timedelta(days=settings.refresh_ttl_days - 1)


async def test_rotation_returns_a_new_token_in_the_same_family(
    sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> None:
    first = await login(sessions, user_id)
    rotated_user, second = await rotate(sessions, first)
    assert rotated_user == user_id and second != first
    rows = await family_rows(sessions, user_id)
    assert len({row.family_id for row in rows}) == 1
    assert sorted(row.revoked_at is None for row in rows) == [False, True]
    await rotate(sessions, second)  # the new token works


@pytest.mark.usefixtures("no_grace")
async def test_reuse_of_a_rotated_token_revokes_the_whole_family(
    sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> None:
    old = await login(sessions, user_id)
    _, current = await rotate(sessions, old)
    with pytest.raises(Unauthenticated):
        await rotate(sessions, old)
    assert all(row.revoked_at is not None for row in await family_rows(sessions, user_id))
    with pytest.raises(Unauthenticated):
        await rotate(sessions, current)  # the legitimate holder is logged out too


@pytest.mark.usefixtures("no_grace")
async def test_reuse_does_not_touch_other_families(
    sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> None:
    old = await login(sessions, user_id)
    other_device = await login(sessions, user_id)
    await rotate(sessions, old)
    with pytest.raises(Unauthenticated):
        await rotate(sessions, old)
    await rotate(sessions, other_device)


async def test_unknown_token_is_rejected(sessions: async_sessionmaker[AsyncSession]) -> None:
    with pytest.raises(Unauthenticated):
        await rotate(sessions, "never-issued")


async def test_expired_token_is_rejected_and_not_rotated(
    sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> None:
    raw = await login(sessions, user_id)
    async with sessions() as session:
        with pytest.raises(Unauthenticated):
            await rotate_refresh_token(
                session, settings, raw, now=datetime.now(UTC) + timedelta(days=31)
            )
    [row] = await family_rows(sessions, user_id)
    assert row.revoked_at is None


@pytest.mark.usefixtures("no_grace")
async def test_two_simultaneous_refreshes_of_one_token_exactly_one_succeeds(
    sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> None:
    raw = await login(sessions, user_id)
    results = await asyncio.gather(
        rotate(sessions, raw), rotate(sessions, raw), return_exceptions=True
    )
    assert sum(isinstance(result, tuple) for result in results) == 1
    assert sum(isinstance(result, Unauthenticated) for result in results) == 1
    # The loser took the reuse path, so the winner's new token is revoked as well.
    rows = await family_rows(sessions, user_id)
    assert len(rows) == 2 and all(row.revoked_at is not None for row in rows)


async def test_replaying_a_rotated_token_after_expiry_still_revokes_the_family(
    sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> None:
    old = await login(sessions, user_id)
    _, current = await rotate(sessions, old)
    async with sessions() as session:
        with pytest.raises(Unauthenticated):
            await rotate_refresh_token(
                session, settings, old, now=datetime.now(UTC) + timedelta(days=31)
            )
    # Revoked is checked before expired, so the stolen-token replay still kills the family.
    assert all(row.revoked_at is not None for row in await family_rows(sessions, user_id))
    with pytest.raises(Unauthenticated):
        await rotate(sessions, current)


async def test_reuse_inside_the_window_follows_at_most_three_rotations(
    sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> None:
    start = datetime.now(UTC)
    chain = [await login(sessions, user_id)]
    for _ in range(REUSE_GRACE_MAX_STEPS):
        async with sessions() as session:
            chain.append((await rotate_refresh_token(session, settings, chain[-1], start))[1])
    async with sessions() as session:
        assert await rotate_refresh_token(
            session, settings, chain[0], start + timedelta(seconds=2)
        ) == (user_id, chain[-1])
    async with sessions() as session:
        await rotate_refresh_token(session, settings, chain[-1], start)  # a fourth rotation
    async with sessions() as session:
        with pytest.raises(Unauthenticated):
            await rotate_refresh_token(session, settings, chain[0], start + timedelta(seconds=2))
    assert all(row.revoked_at is not None for row in await family_rows(sessions, user_id))
