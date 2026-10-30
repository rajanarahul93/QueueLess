import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import Forbidden, NotFound, Unauthenticated
from app.core.security import AccessClaims, decode_access_token
from app.models import Business, BusinessMember, User
from app.services.notifications import SmsProvider


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


def get_clock(request: Request) -> Callable[[], float]:
    clock: Callable[[], float] = request.app.state.clock
    return clock


def get_sms_provider(request: Request) -> SmsProvider:
    sms: SmsProvider = request.app.state.sms
    return sms


def require_trusted_origin(request: Request) -> None:
    """Cookie-authenticated POSTs: a browser always sends Origin, other clients may omit it."""
    origin = request.headers.get("origin")
    if origin is not None and origin not in get_settings(request).cors_origins:
        raise Forbidden("Origin not allowed")


def get_current_user(request: Request) -> AccessClaims:
    """Authentication only: the claims say who is calling, never what they may do."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthenticated("Missing bearer token")
    return decode_access_token(get_settings(request), token)


CurrentUser = Annotated[AccessClaims, Depends(get_current_user)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def account_role(session: AsyncSession, user_id: uuid.UUID) -> str:
    """The role comes from the database, so a demoted or deleted user loses access at once."""
    role = await session.scalar(select(User.role).where(User.id == user_id))
    if role is None:
        raise Unauthenticated("Invalid or expired token")
    return role


def _require_account_role(*roles: str) -> Callable[..., Awaitable[AccessClaims]]:
    async def dependency(user: CurrentUser, session: SessionDep) -> AccessClaims:
        if await account_role(session, user.user_id) not in roles:
            raise Forbidden("Not allowed for this account")
        return user

    return dependency


require_owner_account = _require_account_role("owner")
require_staff_or_owner_account = _require_account_role("owner", "staff")
require_customer_account = _require_account_role("customer")
require_appointment_account = _require_account_role("customer", "owner", "staff")


async def assert_member(
    session: AsyncSession, business_id: uuid.UUID, user_id: uuid.UUID, *roles: str
) -> None:
    """404 unknown business, 403 when the user has no membership with one of `roles`."""
    membership = and_(BusinessMember.business_id == Business.id, BusinessMember.user_id == user_id)
    row = (
        await session.execute(
            select(BusinessMember.role)
            .select_from(Business)
            .outerjoin(BusinessMember, membership)
            .where(Business.id == business_id)
        )
    ).one_or_none()
    if row is None:
        raise NotFound("Business not found")
    if row.role not in roles:
        raise Forbidden("Not allowed for this business")


def require_business_member(*roles: str) -> Callable[..., Awaitable[AccessClaims]]:
    """Checks business_members on every call (FR-27): 404 unknown business, 403 wrong member.

    Admin is not a superuser; only a membership row with one of `roles` passes.
    """

    async def dependency(
        business_id: uuid.UUID, user: CurrentUser, session: SessionDep
    ) -> AccessClaims:
        await assert_member(session, business_id, user.user_id, *roles)
        return user

    return dependency
