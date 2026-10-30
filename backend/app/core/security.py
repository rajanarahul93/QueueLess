import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import Settings
from app.core.errors import Unauthenticated

ALGORITHM = "HS256"
# Instances may have slightly different clocks.
LEEWAY_SEC = 10

# argon2-cffi defaults: Argon2id, RFC 9106 low-memory profile. Immutable, so safe to share.
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    role: str


def create_access_token(
    settings: Settings, user_id: uuid.UUID, role: str, now: datetime | None = None
) -> str:
    # `now` is injectable so tests issue expired tokens instead of sleeping.
    issued = now or datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": issued,
        "exp": issued + timedelta(minutes=settings.jwt_access_ttl_min),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_access_token(settings: Settings, token: str) -> AccessClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],  # pinned: rejects alg=none and any other algorithm
            options={"require": ["sub", "role", "iat", "exp"]},
            leeway=LEEWAY_SEC,
        )
        role = payload["role"]
        if not isinstance(role, str):
            raise ValueError("role must be a string")
        return AccessClaims(user_id=uuid.UUID(payload["sub"]), role=role)
    except (jwt.PyJWTError, ValueError, TypeError) as error:
        raise Unauthenticated("Invalid or expired token") from error


def new_refresh_token() -> tuple[str, str]:
    """Returns (raw token for the client, hash for the database)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    # A fast hash is right here: the input is 256 random bits, so there is nothing to brute-force.
    return hashlib.sha256(raw.encode()).hexdigest()


StreamScope = Literal["queue", "token"]


@dataclass(frozen=True)
class StreamClaims:
    scope: StreamScope
    resource_id: uuid.UUID
    subject_id: uuid.UUID


def create_stream_token(
    settings: Settings,
    scope: StreamScope,
    resource_id: uuid.UUID,
    subject_id: uuid.UUID,
    now: datetime | None = None,
) -> str:
    issued = now or datetime.now(UTC)
    payload = {
        "scope": scope,
        "sid": str(resource_id),
        "sub": str(subject_id),
        "iat": issued,
        "exp": issued + timedelta(seconds=settings.stream_token_ttl_sec),
    }
    return jwt.encode(payload, settings.stream_token_secret, algorithm=ALGORITHM)


def verify_stream_token(
    settings: Settings, token: str, scope: StreamScope, resource_id: uuid.UUID
) -> StreamClaims:
    try:
        payload = jwt.decode(
            token,
            settings.stream_token_secret,
            algorithms=[ALGORITHM],
            options={"require": ["scope", "sid", "sub", "iat", "exp"]},
            leeway=LEEWAY_SEC,
        )
        claims = StreamClaims(
            scope=payload["scope"],
            resource_id=uuid.UUID(payload["sid"]),
            subject_id=uuid.UUID(payload["sub"]),
        )
    except (jwt.PyJWTError, ValueError, TypeError) as error:
        raise Unauthenticated("Invalid or expired stream token") from error
    if claims.scope != scope or claims.resource_id != resource_id:
        raise Unauthenticated("Stream token is for a different resource")
    return claims


def derive_otp_key(settings: Settings) -> bytes:
    """Domain-separated from the JWT signing key, so no new secret is needed."""
    return hmac.new(settings.jwt_secret.encode(), b"queueless/otp/v1", hashlib.sha256).digest()


def otp_mac(key: bytes, label: str, value: str) -> str:
    """Keyed hash for OTP codes and the Redis ids of phone numbers; the label keeps them apart."""
    return hmac.new(key, f"{label}:{value}".encode(), hashlib.sha256).hexdigest()


def derive_idempotency_key(settings: Settings) -> bytes:
    """Domain-separated from the JWT and OTP keys; it hides keys and request bodies in Redis."""
    return hmac.new(
        settings.jwt_secret.encode(), b"queueless/idempotency/v1", hashlib.sha256
    ).digest()
