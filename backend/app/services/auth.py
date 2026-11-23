import asyncio
import hashlib
import hmac
import logging
import math
import secrets
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import LOGIN_LIMIT_WINDOW_SEC, Settings
from app.core.errors import (
    OtpExpired,
    OtpInvalid,
    RateLimited,
    ServiceUnavailable,
    Unauthenticated,
    ValidationFailed,
)
from app.core.rate_limit import RateLimiter
from app.core.security import (
    create_access_token,
    derive_otp_key,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    otp_mac,
    password_needs_rehash,
    successor_refresh_token,
    verify_password,
)
from app.models import RefreshToken, User
from app.services.notifications import SmsProvider

logger = logging.getLogger(__name__)

OTP_REQUEST_PHONE_LIMIT = (5, 3600)
PASSWORD_LOGIN_ROLES = ("owner", "staff", "admin")
DEFAULT_CUSTOMER_NAME = "Customer"
# How many rotations inside the grace window a reused refresh token may be followed through.
REUSE_GRACE_MAX_STEPS = 3
# Verified against when the email is unknown, so both failures cost one argon2 verification.
DUMMY_PASSWORD_HASH = hash_password("dummy-password-for-constant-time-login")

# Atomic: the expiry check and the attempt increment happen before the caller sees the stored mac,
# so parallel guesses cannot exceed the limit. Returns {0} missing/expired, {2, ms left} locked,
# {1, mac} when the caller may compare.
OTP_VERIFY = """
local data = redis.call('HMGET', KEYS[1], 'mac', 'expires_at_ms')
if not data[1] or tonumber(data[2]) <= tonumber(ARGV[1]) then return {0} end
local attempts = redis.call('HINCRBY', KEYS[1], 'attempts', 1)
if attempts > tonumber(ARGV[2]) then return {2, tonumber(data[2]) - tonumber(ARGV[1])} end
return {1, data[1]}
"""


@dataclass(frozen=True)
class AuthResult:
    user: User
    access_token: str
    refresh_token: str


def _now(clock: Callable[[], float]) -> datetime:
    return datetime.fromtimestamp(clock(), UTC)


def _add_refresh_token(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    family_id: uuid.UUID,
    now: datetime,
    issued: tuple[str, str],
) -> str:
    raw, token_hash = issued
    session.add(
        RefreshToken(
            user_id=user_id,
            family_id=family_id,
            token_hash=token_hash,
            expires_at=now + timedelta(days=settings.refresh_ttl_days),
        )
    )
    return raw


async def issue_refresh_token(
    session: AsyncSession, settings: Settings, user_id: uuid.UUID, now: datetime | None = None
) -> str:
    """Starts a new token family (a login). Returns the raw token; only its hash is stored."""
    raw = _add_refresh_token(
        session, settings, user_id, uuid.uuid4(), now or datetime.now(UTC), new_refresh_token()
    )
    await session.commit()
    return raw


async def rotate_refresh_token(
    session: AsyncSession, settings: Settings, raw_token: str, now: datetime | None = None
) -> tuple[uuid.UUID, str]:
    """Swaps a refresh token for a new one in the same family; returns (user_id, new raw token).

    Presenting a token that was already rotated revokes its whole family, unless it was rotated
    within refresh_reuse_grace_sec (a retry after a lost response): then it gets the same live
    successor again. The row lock makes two simultaneous refreshes of one token serialize: one
    rotates, the other sees it revoked and takes the reuse path.
    """
    now = now or datetime.now(UTC)
    token = (
        await session.execute(
            select(RefreshToken)
            .where(RefreshToken.token_hash == hash_refresh_token(raw_token))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if token is None:
        await session.rollback()  # release the row lock
        raise Unauthenticated("Invalid refresh token")
    if token.revoked_at is not None:
        successor = await _grace_successor(session, settings, token, raw_token, now)
        if successor is not None:
            user_id, family_id = token.user_id, token.family_id
            await session.rollback()  # nothing changed; releases the row lock
            logger.info("refresh reuse within grace window family_id=%s", family_id)
            return user_id, successor
        # Checked before expiry: replaying a rotated token after it expired must still revoke.
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == token.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await session.commit()  # must persist before raising, or the revocation is rolled back
        raise Unauthenticated("Invalid refresh token")
    if token.expires_at <= now:
        await session.rollback()
        raise Unauthenticated("Invalid refresh token")
    token.revoked_at = now
    new_raw = _add_refresh_token(
        session,
        settings,
        token.user_id,
        token.family_id,
        now,
        successor_refresh_token(settings, raw_token),
    )
    user_id = token.user_id
    await session.commit()
    return user_id, new_raw


async def _grace_successor(
    session: AsyncSession, settings: Settings, token: RefreshToken, raw_token: str, now: datetime
) -> str | None:
    """The live token that rotations of raw_token led to, if each of those rotations happened
    within the grace window and there were at most REUSE_GRACE_MAX_STEPS; otherwise None."""
    window = timedelta(seconds=settings.refresh_reuse_grace_sec)
    raw, row = raw_token, token
    for _ in range(REUSE_GRACE_MAX_STEPS):
        if not window or row.revoked_at is None or now - row.revoked_at > window:
            return None
        raw, successor_hash = successor_refresh_token(settings, raw)
        # Not locked: only read. A logout or revocation ends the chain at a revoked row.
        found = (
            await session.execute(
                select(RefreshToken).where(
                    RefreshToken.token_hash == successor_hash,
                    RefreshToken.family_id == token.family_id,
                )
            )
        ).scalar_one_or_none()
        if found is None or found.expires_at <= now:
            return None
        if found.revoked_at is None:
            return raw
        row = found
    return None


@contextmanager
def _redis_fails_closed() -> Iterator[None]:
    try:
        yield
    except RedisError as error:
        logger.error("OTP store cannot reach Redis, rejecting request", exc_info=error)
        raise ServiceUnavailable("OTP service unavailable") from error


def _otp_key(phone_id: str) -> str:
    return f"otp:{phone_id}"


async def request_otp(
    redis: Redis,
    limiter: RateLimiter,
    sms: SmsProvider,
    settings: Settings,
    clock: Callable[[], float],
    phone: str,
) -> str | None:
    """Same outcome whether or not an account exists for the phone.

    Returns the code ONLY for the demo provider, which sends nothing and so has the endpoint show
    it; every other provider returns None. The settings allow that provider only with
    APP_ENV=demo, and every limit below applies to it as to any other."""
    key = derive_otp_key(settings)
    phone_id = otp_mac(key, "phone", phone)
    await limiter.check("otp_cooldown", phone_id, 1, settings.otp_resend_cooldown_sec)
    await limiter.check("otp_phone", phone_id, *OTP_REQUEST_PHONE_LIMIT)
    code = f"{secrets.randbelow(10**6):06d}"
    expires_at_ms = int(clock() * 1000) + settings.otp_ttl_sec * 1000
    with _redis_fails_closed():
        # Overwrites any earlier code and resets its attempts. Redis TTL is only cleanup; expiry
        # is expires_at_ms against the injected clock.
        async with redis.pipeline(transaction=True) as pipe:
            pipe.hset(
                _otp_key(phone_id),
                mapping={
                    "mac": otp_mac(key, "code", f"{phone_id}:{code}"),
                    "attempts": 0,
                    "expires_at_ms": expires_at_ms,
                },
            )
            pipe.pexpire(_otp_key(phone_id), settings.otp_ttl_sec * 1000 + 60_000)
            await pipe.execute()
    await sms.send_otp(phone, code)
    return code if settings.otp_provider == "demo" else None


async def consume_otp(
    redis: Redis, settings: Settings, clock: Callable[[], float], phone: str, code: str
) -> None:
    """Raises unless `code` is the live code for `phone`; a correct code works exactly once."""
    key = derive_otp_key(settings)
    phone_id = otp_mac(key, "phone", phone)
    now_ms = int(clock() * 1000)
    with _redis_fails_closed():
        script = redis.register_script(OTP_VERIFY)
        status, *rest = await script(
            keys=[_otp_key(phone_id)], args=[now_ms, settings.otp_max_attempts]
        )
        if status == 0:
            raise OtpExpired
        if status == 2:
            raise RateLimited(math.ceil(rest[0] / 1000))
        expected = otp_mac(key, "code", f"{phone_id}:{code}").encode()
        if not hmac.compare_digest(rest[0], expected):
            raise OtpInvalid
        # DEL returns 1 for exactly one caller, so parallel correct verifies yield one session.
        if await redis.delete(_otp_key(phone_id)) == 0:
            raise OtpExpired


async def upsert_customer(
    session: AsyncSession, phone: str, name: str | None, now: datetime
) -> User | None:
    """None when the phone belongs to a non-customer account. The caller commits."""
    statement = (
        pg_insert(User)
        .values(
            name=name or DEFAULT_CUSTOMER_NAME, phone=phone, role="customer", phone_verified_at=now
        )
        .on_conflict_do_update(
            index_elements=[User.phone],
            set_={"phone_verified_at": now},  # the name of an existing user is never overwritten
            where=User.role == "customer",
        )
        .returning(User)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def _start_session(
    session: AsyncSession, settings: Settings, user: User, now: datetime
) -> AuthResult:
    raw = await issue_refresh_token(session, settings, user.id, now)
    return AuthResult(user, create_access_token(settings, user.id, user.role, now), raw)


async def verify_otp(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    clock: Callable[[], float],
    phone: str,
    code: str,
    name: str | None,
) -> AuthResult:
    await consume_otp(redis, settings, clock, phone, code)
    now = _now(clock)
    user = await upsert_customer(session, phone, name, now)
    if user is None:
        # A staff or owner phone cannot become a customer session; same error as a wrong code.
        await session.rollback()
        raise OtpInvalid
    return await _start_session(session, settings, user, now)


async def create_password_user(
    session: AsyncSession, name: str, email: str, password: str, role: str
) -> User:
    """Inserts a NEW user without committing; a taken email is a 422, never an attach."""
    password_hash = await asyncio.to_thread(hash_password, password)
    statement = (
        pg_insert(User)
        .values(name=name, email=email, password_hash=password_hash, role=role)
        .on_conflict_do_nothing(index_elements=[User.email])
        .returning(User)
    )
    user = (await session.execute(statement)).scalar_one_or_none()
    if user is None:
        raise ValidationFailed(["body", "email"], "Email already registered")
    return user


async def register_owner(session: AsyncSession, name: str, email: str, password: str) -> User:
    user = await create_password_user(session, name, email, password, "owner")
    await session.commit()
    return user


async def login(
    session: AsyncSession,
    limiter: RateLimiter,
    settings: Settings,
    clock: Callable[[], float],
    email: str,
    password: str,
) -> AuthResult:
    await limiter.check(
        "login_email",
        hashlib.sha256(email.lower().encode()).hexdigest(),
        settings.login_email_limit,
        LOGIN_LIMIT_WINDOW_SEC,
    )
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    stored_hash = (
        user.password_hash if user is not None and user.role in PASSWORD_LOGIN_ROLES else None
    )
    matches = await asyncio.to_thread(verify_password, stored_hash or DUMMY_PASSWORD_HASH, password)
    if user is None or stored_hash is None or not matches:
        raise Unauthenticated("Invalid email or password")
    if password_needs_rehash(stored_hash):
        user.password_hash = await asyncio.to_thread(hash_password, password)
    return await _start_session(session, settings, user, _now(clock))


async def refresh_session(
    session: AsyncSession, settings: Settings, clock: Callable[[], float], raw_token: str
) -> AuthResult:
    now = _now(clock)
    user_id, new_raw = await rotate_refresh_token(session, settings, raw_token, now)
    # The role comes from the database now, not from the old access token.
    user = await session.get(User, user_id)
    if user is None:
        raise Unauthenticated("Invalid refresh token")
    return AuthResult(user, create_access_token(settings, user.id, user.role, now), new_raw)


async def revoke_session(session: AsyncSession, raw_token: str, now: datetime) -> None:
    """Revokes the token's whole family. Unknown tokens are ignored, so logout is idempotent."""
    family_id = (
        select(RefreshToken.family_id)
        .where(RefreshToken.token_hash == hash_refresh_token(raw_token))
        .scalar_subquery()
    )
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await session.commit()
