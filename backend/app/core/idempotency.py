"""Idempotency-Key for JSON writes (FR-5).

Flow: the `idempotency` dependency runs after authentication, authorization and the rate limit.
It atomically claims the key (SET NX) or replays/rejects. `IdempotentRoute` wraps the handler:
a 2xx response is stored for replay; any other outcome (error status, exception, cancellation)
releases the claim so a retry is always safe.
"""

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request, Response
from fastapi.routing import APIRoute
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.dependencies import CurrentUser, get_redis, get_settings
from app.core.errors import IdempotencyConflict, ServiceUnavailable, ValidationFailed
from app.core.security import derive_idempotency_key, otp_mac

logger = logging.getLogger(__name__)

CLAIM_TTL_MS = 60_000  # a crashed request cannot block its key for the full retention period
KEY_PATTERN = re.compile(r"^[\x20-\x7e]{1,255}$")
HEADER = "idempotency-key"

# Replaces the value only if it is still this request's pending claim ("p:<token>:...").
CLAIM_SWAP = """
local value = redis.call('GET', KEYS[1])
if value and string.sub(value, 1, string.len(ARGV[1])) == ARGV[1] then
  if ARGV[2] == '' then
    redis.call('DEL', KEYS[1])
  else
    redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
  end
  return 1
end
return 0
"""


class Replay(Exception):
    """Raised by the dependency to answer with a stored response instead of running the handler."""

    def __init__(self, status: int, body: str, media_type: str) -> None:
        self.status, self.body, self.media_type = status, body, media_type


@dataclass(frozen=True)
class Claim:
    redis_key: str
    token: str  # "p:<token>:", the prefix of this request's pending value
    fingerprint: str


def replay_response(error: Replay) -> Response:
    return Response(
        error.body,
        status_code=error.status,
        media_type=error.media_type,
        headers={"Idempotent-Replayed": "true"},
    )


async def handle_replay(request: Request, error: Replay) -> Response:
    return replay_response(error)


def body_fingerprint(settings: Settings, body: bytes) -> str:
    try:  # canonical JSON, so key order and whitespace do not matter
        canonical = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"))
    except ValueError:  # not JSON: hash the raw bytes; the request will fail validation
        canonical = body.decode("latin-1")
    return otp_mac(derive_idempotency_key(settings), "body", canonical)


def claim_key(settings: Settings, user_id: uuid.UUID, method: str, path: str, key: str) -> str:
    """The Redis key is a keyed hash, so neither the client's key nor the path is stored."""
    scope = f"{user_id}\n{method}\n{path}\n{key}"
    return f"idem:{otp_mac(derive_idempotency_key(settings), 'key', scope)}"


async def idempotency(
    request: Request,
    user: CurrentUser,
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    header = request.headers.get(HEADER)
    if header is None:
        return
    if not KEY_PATTERN.fullmatch(header):
        raise ValidationFailed(["header", HEADER], "Must be 1 to 255 printable ASCII characters")
    redis_key = claim_key(settings, user.user_id, request.method, request.url.path, header)
    fingerprint = body_fingerprint(settings, await request.body())
    token = uuid.uuid4().hex
    pending = f"p:{token}:{fingerprint}"
    try:
        for _ in range(3):  # the key can expire between SET NX and GET, so retry the claim
            if await redis.set(redis_key, pending, nx=True, px=CLAIM_TTL_MS):
                request.state.idempotency = Claim(redis_key, f"p:{token}:", fingerprint)
                return
            stored = await redis.get(redis_key)
            if stored is not None:
                _answer_existing(stored.decode(), fingerprint)
    except RedisError as error:
        logger.error("idempotency store unavailable, rejecting request", exc_info=error)
        raise ServiceUnavailable("Idempotency store unavailable") from error
    raise ServiceUnavailable("Idempotency key could not be claimed")


def _answer_existing(stored: str, fingerprint: str) -> None:
    if stored.startswith("p:"):
        same_body = stored.endswith(f":{fingerprint}")
        if same_body:
            raise IdempotencyConflict(
                "A request with this Idempotency-Key is still in progress", in_progress=True
            )
    else:
        record = json.loads(stored)
        if record["fingerprint"] == fingerprint:
            raise Replay(record["status"], record["body"], record["media_type"])
    raise IdempotencyConflict("This Idempotency-Key was used with a different request body")


async def _swap(redis: Redis, claim: Claim, value: str, ttl_ms: int) -> None:
    script = redis.register_script(CLAIM_SWAP)
    await script(keys=[claim.redis_key], args=[claim.token, value, ttl_ms])


async def _release(redis: Redis, claim: Claim) -> None:
    try:
        await asyncio.shield(_swap(redis, claim, "", 0))
    except RedisError as error:  # the claim expires on its own after CLAIM_TTL_MS
        logger.error("could not release idempotency claim", exc_info=error)


async def _store(request: Request, claim: Claim, response: Response) -> None:
    settings = get_settings(request)
    record = json.dumps(
        {
            "fingerprint": claim.fingerprint,
            "status": response.status_code,
            "body": bytes(response.body).decode(),
            "media_type": response.media_type or "application/json",
        }
    )
    try:
        await _swap(get_redis(request), claim, record, settings.idempotency_ttl_sec * 1000)
    except RedisError as error:
        # The handler already committed: return its real response and leave the claim to expire.
        logger.error("could not store idempotent response", exc_info=error)


class IdempotentRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                response = await original(request)
            except Replay:
                raise
            except BaseException:
                claim = getattr(request.state, "idempotency", None)
                if claim is not None:
                    await _release(get_redis(request), claim)
                raise
            claim = getattr(request.state, "idempotency", None)
            if claim is not None:
                if 200 <= response.status_code < 300:
                    await _store(request, claim, response)
                else:
                    await _release(get_redis(request), claim)
            return response

        return handler
