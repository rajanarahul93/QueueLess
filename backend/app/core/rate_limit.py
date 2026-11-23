import ipaddress
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import LOGIN_LIMIT_WINDOW_SEC, Settings
from app.core.dependencies import CurrentUser, get_clock, get_redis, get_settings
from app.core.errors import RateLimited, ServiceUnavailable

logger = logging.getLogger(__name__)

# Sliding-window log in a sorted set (score = request time in ms). One script keeps
# trim, count and add atomic. Denied requests are not recorded, so hammering does not
# extend the block. Returns {allowed, retry_after_ms}.
SLIDING_WINDOW = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return {0, tonumber(oldest[2]) + window - now}
end
redis.call('ZADD', KEYS[1], now, ARGV[4])
redis.call('PEXPIRE', KEYS[1], window)
return {1, 0}
"""


class RateLimiter:
    def __init__(self, redis: Redis, clock: Callable[[], float] = time.time) -> None:
        self._script = redis.register_script(SLIDING_WINDOW)
        self._clock = clock

    async def check(self, scope: str, identifier: str, limit: int, window_sec: int) -> None:
        """Records one hit, or raises RateLimited. Fails closed if Redis is unreachable."""
        now_ms = int(self._clock() * 1000)
        member = f"{now_ms}-{uuid.uuid4().hex}"
        try:
            allowed, retry_after_ms = await self._script(
                keys=[f"rl:{scope}:{identifier}"],
                args=[now_ms, window_sec * 1000, limit, member],
            )
        except RedisError as error:
            logger.error("rate limiter cannot reach Redis, rejecting request", exc_info=error)
            raise ServiceUnavailable("Rate limiter unavailable") from error
        if not allowed:
            raise RateLimited(math.ceil(retry_after_ms / 1000))


def get_rate_limiter(
    redis: Annotated[Redis, Depends(get_redis)],
    clock: Annotated[Callable[[], float], Depends(get_clock)],
) -> RateLimiter:
    return RateLimiter(redis, clock)


def client_ip(request: Request) -> str:
    """The address that rate limits are counted against.

    By default the address of the connection itself. When CLIENT_IP_HEADER is set, that one
    header's value is used if it is a single valid IP address, else the connection's address.
    Safe ONLY behind a proxy that overwrites that header on every request (Caddy with
    `header_up X-Real-IP {remote_host}`, Railway's X-Real-IP): a client can send any value of a
    header the proxy passes through. x-forwarded-for can never be configured here, because it
    is a client-supplied list that every proxy only appends to.
    """
    peer = request.client.host if request.client else "unknown"
    header = request.app.state.settings.client_ip_header
    if not header:
        return peer
    try:
        address = ipaddress.ip_address(request.headers.get(header, ""))
    except ValueError:
        return peer
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id:  # fe80::1%a, fe80::1%b: one client could mint endless buckets
            return peer
        address = address.ipv4_mapped or address  # ::ffff:1.2.3.4 is the same client as 1.2.3.4
    return str(address)


def rate_limit(
    scope: str,
    *,
    limit: int,
    window_sec: int,
    key: Callable[[Request], str] = client_ip,
) -> Callable[..., Awaitable[None]]:
    """Dependency factory. Limits that depend on the request body call RateLimiter directly."""

    async def dependency(
        request: Request, limiter: Annotated[RateLimiter, Depends(get_rate_limiter)]
    ) -> None:
        await limiter.check(scope, key(request), limit, window_sec)

    return dependency


def user_rate_limit(scope: str, *, limit: int, window_sec: int) -> Callable[..., Awaitable[None]]:
    """Per authenticated user; list it after the auth and membership dependencies."""

    async def dependency(
        user: CurrentUser, limiter: Annotated[RateLimiter, Depends(get_rate_limiter)]
    ) -> None:
        await limiter.check(scope, str(user.user_id), limit, window_sec)

    return dependency


async def login_ip_rate_limit(
    request: Request,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Per IP; the limit is a setting so the e2e stack can raise it (never a deployment)."""
    await limiter.check(
        "login_ip", client_ip(request), settings.login_ip_limit, LOGIN_LIMIT_WINDOW_SEC
    )


async def token_create_rate_limit(
    user: CurrentUser,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """One per-user bucket shared by walk-in join and check-in (both create tokens)."""
    await limiter.check(
        "token_create_user", str(user.user_id), settings.join_rate_limit_per_hour, 3600
    )
