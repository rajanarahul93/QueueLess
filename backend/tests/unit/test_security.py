import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from argon2 import PasswordHasher

from app.core.errors import Unauthenticated
from app.core.security import (
    create_access_token,
    create_stream_token,
    decode_access_token,
    derive_idempotency_key,
    derive_otp_key,
    derive_refresh_successor_key,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    password_needs_rehash,
    successor_refresh_token,
    verify_password,
    verify_stream_token,
)
from tests.helpers import TEST_JWT_SECRET, TEST_STREAM_SECRET, make_settings

settings = make_settings()
USER_ID = uuid.uuid4()
RESOURCE_ID = uuid.uuid4()
PAST = datetime(2020, 1, 1, tzinfo=UTC)


def test_password_hash_and_verify() -> None:
    password_hash = hash_password("correct horse")
    assert password_hash.startswith("$argon2id$")
    assert verify_password(password_hash, "correct horse")
    assert not verify_password(password_hash, "wrong")
    assert not verify_password("not-a-hash", "correct horse")
    assert not password_needs_rehash(password_hash)


def test_hash_with_weaker_parameters_needs_rehash() -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("pw")
    assert verify_password(weak, "pw")
    assert password_needs_rehash(weak)


def test_access_token_round_trip() -> None:
    claims = decode_access_token(settings, create_access_token(settings, USER_ID, "owner"))
    assert (claims.user_id, claims.role) == (USER_ID, "owner")


def test_access_token_lasts_the_configured_minutes() -> None:
    token = create_access_token(settings, USER_ID, "owner", now=PAST)
    payload = jwt.decode(token, options={"verify_signature": False})
    assert payload["exp"] - payload["iat"] == settings.jwt_access_ttl_min * 60


def encode(payload: dict[str, object], secret: str = TEST_JWT_SECRET, alg: str = "HS256") -> str:
    return jwt.encode(payload, secret, algorithm=alg)


def valid_payload() -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "sub": str(USER_ID),
        "role": "owner",
        "iat": now,
        "exp": now + timedelta(minutes=15),
    }


def test_expired_access_token_is_rejected() -> None:
    with pytest.raises(Unauthenticated):
        decode_access_token(settings, create_access_token(settings, USER_ID, "owner", now=PAST))


def test_access_token_with_wrong_signature_is_rejected() -> None:
    with pytest.raises(Unauthenticated):
        decode_access_token(settings, encode(valid_payload(), secret=TEST_STREAM_SECRET))


def test_tampered_access_token_is_rejected() -> None:
    header, _, signature = create_access_token(settings, USER_ID, "staff").split(".")
    forged = encode(valid_payload() | {"role": "owner"}).split(".")[1]
    with pytest.raises(Unauthenticated):
        decode_access_token(settings, f"{header}.{forged}.{signature}")


def test_alg_none_is_rejected() -> None:
    unsigned = jwt.encode(valid_payload(), None, algorithm="none")
    with pytest.raises(Unauthenticated):
        decode_access_token(settings, unsigned)


def test_other_hmac_algorithm_is_rejected() -> None:
    with pytest.raises(Unauthenticated):
        decode_access_token(settings, encode(valid_payload(), alg="HS384"))


@pytest.mark.parametrize("claim", ["sub", "role", "iat", "exp"])
def test_access_token_missing_a_claim_is_rejected(claim: str) -> None:
    payload = valid_payload()
    del payload[claim]
    with pytest.raises(Unauthenticated):
        decode_access_token(settings, encode(payload))


def test_access_token_with_malformed_claims_is_rejected() -> None:
    with pytest.raises(Unauthenticated):
        decode_access_token(settings, encode(valid_payload() | {"sub": "not-a-uuid"}))
    with pytest.raises(Unauthenticated):
        decode_access_token(settings, encode(valid_payload() | {"role": 7}))


def test_refresh_tokens_are_random_and_stored_hashed() -> None:
    (raw, stored), (other, _) = new_refresh_token(), new_refresh_token()
    assert raw != other
    assert len(raw) >= 43  # 32 random bytes, urlsafe base64
    assert stored == hash_refresh_token(raw) != raw


def test_stream_token_round_trip() -> None:
    token = create_stream_token(settings, "queue", RESOURCE_ID, USER_ID)
    claims = verify_stream_token(settings, token, "queue", RESOURCE_ID)
    assert (claims.scope, claims.resource_id, claims.subject_id) == ("queue", RESOURCE_ID, USER_ID)


def test_expired_stream_token_is_rejected() -> None:
    token = create_stream_token(settings, "queue", RESOURCE_ID, USER_ID, now=PAST)
    with pytest.raises(Unauthenticated):
        verify_stream_token(settings, token, "queue", RESOURCE_ID)


def test_stream_token_for_wrong_scope_or_resource_is_rejected() -> None:
    token = create_stream_token(settings, "queue", RESOURCE_ID, USER_ID)
    with pytest.raises(Unauthenticated):
        verify_stream_token(settings, token, "token", RESOURCE_ID)
    with pytest.raises(Unauthenticated):
        verify_stream_token(settings, token, "queue", uuid.uuid4())


def test_tampered_stream_token_is_rejected() -> None:
    header, _, signature = create_stream_token(settings, "queue", RESOURCE_ID, USER_ID).split(".")
    other = create_stream_token(settings, "queue", uuid.uuid4(), USER_ID).split(".")[1]
    with pytest.raises(Unauthenticated):
        verify_stream_token(settings, f"{header}.{other}.{signature}", "queue", RESOURCE_ID)


def test_access_and_stream_tokens_are_not_interchangeable() -> None:
    access = create_access_token(settings, USER_ID, "owner")
    stream = create_stream_token(settings, "queue", RESOURCE_ID, USER_ID)
    with pytest.raises(Unauthenticated):
        verify_stream_token(settings, access, "queue", RESOURCE_ID)
    with pytest.raises(Unauthenticated):
        decode_access_token(settings, stream)


def test_refresh_successor_derivation_has_its_own_label() -> None:
    derived = derive_refresh_successor_key(settings)
    assert derived not in (
        derive_otp_key(settings),
        derive_idempotency_key(settings),
        TEST_JWT_SECRET.encode(),
    )
    other = make_settings(jwt_secret=TEST_JWT_SECRET[::-1])
    assert derive_refresh_successor_key(other) != derived


def test_refresh_successors_are_distinct_never_the_old_one_and_stored_hashed() -> None:
    olds = [new_refresh_token()[0] for _ in range(500)]
    successors = [successor_refresh_token(settings, old) for old in olds]
    assert len({raw for raw, _ in successors}) == len(olds)
    for old, (raw, stored) in zip(olds, successors, strict=True):
        assert raw != old and stored == hash_refresh_token(raw) != raw
        assert len(raw) == len(old) == 43
        assert successor_refresh_token(settings, old) == (raw, stored)  # deterministic
