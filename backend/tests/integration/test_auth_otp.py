import asyncio
import logging
from collections.abc import Awaitable
from typing import cast

import jwt
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.core.errors import ServiceUnavailable
from app.core.rate_limit import RateLimiter
from app.models import RefreshToken, User
from app.services import auth as auth_service
from app.services.notifications import MockSmsProvider
from tests.integration.api import Api, new_phone, open_api

pytestmark = pytest.mark.anyio

COOLDOWN = 31  # seconds; just past the 30 s resend cooldown


async def count_users(api: Api, phone: str) -> int:
    async with api.sessions() as session:
        return (
            await session.execute(select(func.count()).select_from(User).where(User.phone == phone))
        ).scalar_one()


async def test_request_is_identical_for_known_and_unknown_phones(api: Api) -> None:
    known = new_phone()
    await api.create_user("customer", phone=known)
    unknown = await api.request_otp(new_phone())
    exists = await api.request_otp(known)
    assert (unknown.status_code, unknown.json()) == (exists.status_code, exists.json()) == (202, {})
    assert dict(unknown.headers).keys() == dict(exists.headers).keys()
    assert len(api.sms.sent) == 2


async def test_redis_holds_no_raw_phone_and_no_plain_code(api: Api) -> None:
    phone = new_phone()
    await api.request_otp(phone)
    code = api.last_code()
    redis = api.redis
    keys = [key async for key in redis.scan_iter("otp:*")]
    assert len(keys) == 1
    values = await cast(Awaitable[list[bytes]], redis.hvals(keys[0]))
    dump = keys[0] + b"".join(values)
    assert phone.encode() not in dump and phone[1:].encode() not in dump
    assert code.encode() not in dump  # only the keyed hash of the code is stored


async def test_resend_cooldown(api: Api) -> None:
    phone = new_phone()
    assert (await api.request_otp(phone)).status_code == 202
    denied = await api.request_otp(phone)
    assert denied.status_code == 429 and denied.json()["code"] == "RATE_LIMITED"
    assert 0 < int(denied.headers["retry-after"]) <= 30
    api.clock.now += COOLDOWN
    assert (await api.request_otp(phone)).status_code == 202


async def test_per_phone_limit_is_five_per_hour(api: Api) -> None:
    phone = new_phone()
    for _ in range(5):
        assert (await api.request_otp(phone)).status_code == 202
        api.clock.now += COOLDOWN
    denied = await api.request_otp(phone)
    assert denied.status_code == 429
    assert int(denied.headers["retry-after"]) > 30  # the hourly window, not the cooldown
    api.clock.now += 3600
    assert (await api.request_otp(phone)).status_code == 202


async def test_per_ip_limit_is_twenty_per_hour(api: Api) -> None:
    for _ in range(20):
        assert (await api.request_otp(new_phone())).status_code == 202
    assert (await api.request_otp(new_phone())).status_code == 429


async def test_invalid_phone_is_a_validation_error(api: Api) -> None:
    response = await api.request_otp("9876543210")
    assert response.status_code == 422 and response.json()["code"] == "VALIDATION_ERROR"


async def test_verify_creates_a_customer_and_issues_a_session(api: Api) -> None:
    phone = new_phone()
    await api.request_otp(phone)
    response = await api.verify_otp(phone, api.last_code(), name="Asha")
    assert response.status_code == 200
    body = response.json()
    assert body["user"] | {"id": None} == {
        "id": None,
        "name": "Asha",
        "email": None,
        "phone": phone,
        "role": "customer",
    }
    claims = jwt.decode(body["access_token"], api.settings.jwt_secret, algorithms=["HS256"])
    assert claims["role"] == "customer" and claims["sub"] == body["user"]["id"]
    refresh = api.client.cookies["refresh_token"]
    assert refresh not in response.text and "password_hash" not in response.text
    async with api.sessions() as session:
        user = (await session.execute(select(User).where(User.phone == phone))).scalar_one()
        assert user.phone_verified_at is not None


async def test_default_name_and_existing_name_is_never_overwritten(api: Api) -> None:
    phone = new_phone()
    await api.request_otp(phone)
    first = await api.verify_otp(phone, api.last_code())
    assert first.json()["user"]["name"] == "Customer"
    api.clock.now += COOLDOWN
    await api.request_otp(phone)
    second = await api.verify_otp(phone, api.last_code(), name="Other")
    assert second.json()["user"]["name"] == "Customer"
    assert second.json()["user"]["id"] == first.json()["user"]["id"]


async def test_wrong_code_is_otp_invalid_and_never_creates_a_user(api: Api) -> None:
    phone = new_phone()
    await api.request_otp(phone)
    wrong = "000000" if api.last_code() != "000000" else "000001"
    response = await api.verify_otp(phone, wrong)
    assert response.status_code == 400 and response.json()["code"] == "OTP_INVALID"
    assert await count_users(api, phone) == 0


async def test_expired_and_never_requested_codes_are_otp_expired(api: Api) -> None:
    phone = new_phone()
    never = await api.verify_otp(phone, "123456")
    assert never.status_code == 400 and never.json()["code"] == "OTP_EXPIRED"
    await api.request_otp(phone)
    code = api.last_code()
    api.clock.now += api.settings.otp_ttl_sec + 1
    expired = await api.verify_otp(phone, code)
    assert expired.status_code == 400 and expired.json()["code"] == "OTP_EXPIRED"
    assert await count_users(api, phone) == 0


async def test_attempt_lockout_rejects_even_the_correct_code(api: Api) -> None:
    phone = new_phone()
    await api.request_otp(phone)
    code = api.last_code()
    wrong = "000000" if code != "000000" else "000001"
    for _ in range(api.settings.otp_max_attempts):
        assert (await api.verify_otp(phone, wrong)).json()["code"] == "OTP_INVALID"
    locked = await api.verify_otp(phone, code)
    assert locked.status_code == 429 and locked.json()["code"] == "RATE_LIMITED"
    assert 0 < int(locked.headers["retry-after"]) <= api.settings.otp_ttl_sec
    assert await count_users(api, phone) == 0
    api.clock.now += api.settings.otp_ttl_sec + 1
    assert (await api.verify_otp(phone, code)).json()["code"] == "OTP_EXPIRED"


async def test_a_new_request_replaces_the_old_code_and_resets_attempts(api: Api) -> None:
    phone = new_phone()
    await api.request_otp(phone)
    old = api.last_code()
    api.clock.now += COOLDOWN
    await api.request_otp(phone)
    new = api.last_code()
    if old != new:  # 1 in 10^6 the codes collide
        assert (await api.verify_otp(phone, old)).json()["code"] == "OTP_INVALID"
    assert (await api.verify_otp(phone, new)).status_code == 200


async def test_code_is_single_use(api: Api) -> None:
    phone = new_phone()
    await api.request_otp(phone)
    code = api.last_code()
    assert (await api.verify_otp(phone, code)).status_code == 200
    again = await api.verify_otp(phone, code)
    assert again.status_code == 400 and again.json()["code"] == "OTP_EXPIRED"


async def test_two_parallel_verifies_of_the_right_code_give_one_session(api: Api) -> None:
    phone = new_phone()
    await api.request_otp(phone)
    code = api.last_code()
    other_device = api.new_client()
    async with other_device:
        responses = await asyncio.gather(
            api.verify_otp(phone, code), api.verify_otp(phone, code, client=other_device)
        )
    assert sorted(r.status_code for r in responses) == [200, 400]
    assert await count_users(api, phone) == 1
    async with api.sessions() as session:
        tokens = (
            await session.execute(select(func.count()).select_from(RefreshToken))
        ).scalar_one()
        user_id = (await session.execute(select(User.id).where(User.phone == phone))).scalar_one()
        owned = (
            await session.execute(
                select(func.count())
                .select_from(RefreshToken)
                .where(RefreshToken.user_id == user_id)
            )
        ).scalar_one()
    assert owned == 1 and tokens >= 1


async def test_concurrent_first_time_upserts_create_exactly_one_user(api: Api) -> None:
    phone = new_phone()
    now = auth_service._now(api.clock)

    async def upsert() -> User | None:
        async with api.sessions() as session:
            user = await auth_service.upsert_customer(session, phone, None, now)
            await session.commit()
            return user

    first, second = await asyncio.gather(upsert(), upsert())
    assert first is not None and second is not None and first.id == second.id
    assert await count_users(api, phone) == 1


@pytest.mark.parametrize("role", ["staff", "owner", "admin"])
async def test_a_non_customer_phone_cannot_log_in_by_otp(api: Api, role: str) -> None:
    phone = new_phone()
    await api.create_user(role, phone=phone, email=f"{role}-{phone[1:]}@example.test")
    await api.request_otp(phone)
    assert len(api.sms.sent) == 1  # the code is still sent, so the 202 reveals nothing
    response = await api.verify_otp(phone, api.last_code())
    assert response.status_code == 400 and response.json()["code"] == "OTP_INVALID"
    assert "refresh_token" not in api.client.cookies
    async with api.sessions() as session:
        user = (await session.execute(select(User).where(User.phone == phone))).scalar_one()
        assert user.role == role and user.phone_verified_at is None
        count = (
            await session.execute(
                select(func.count())
                .select_from(RefreshToken)
                .where(RefreshToken.user_id == user.id)
            )
        ).scalar_one()
    assert count == 0


async def test_verify_per_ip_limit(api: Api) -> None:
    for _ in range(30):
        assert (await api.verify_otp(new_phone(), "123456")).status_code == 400
    assert (await api.verify_otp(new_phone(), "123456")).status_code == 429


async def test_otp_store_fails_closed_when_redis_is_down(api: Api) -> None:
    dead = Redis.from_url("redis://localhost:1/0")
    try:
        limiter = RateLimiter(api.redis, api.clock)
        with pytest.raises(ServiceUnavailable):
            await auth_service.request_otp(
                dead, limiter, api.sms, api.settings, api.clock, new_phone()
            )
        with pytest.raises(ServiceUnavailable):
            await auth_service.consume_otp(dead, api.settings, api.clock, new_phone(), "123456")
    finally:
        await dead.aclose()


async def test_http_returns_503_when_redis_is_down(migrated_url: str) -> None:
    async with open_api(migrated_url, "redis://localhost:1/0", flush_redis=False) as down:
        request = await down.client.post("/v1/auth/otp/request", json={"phone": new_phone()})
        verify = await down.client.post(
            "/v1/auth/otp/verify", json={"phone": new_phone(), "code": "123456"}
        )
    assert request.status_code == verify.status_code == 503
    assert request.json()["code"] == verify.json()["code"] == "SERVICE_UNAVAILABLE"


async def test_mock_provider_logs_the_code_only_when_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        await MockSmsProvider(log_codes=True).send_otp("+919876543210", "123456")
        quiet = MockSmsProvider(log_codes=False)
        await quiet.send_otp("+919876543210", "654321")
    assert "123456" in caplog.text and "654321" not in caplog.text
    assert "+919876543210" not in caplog.text  # R5.2: no full phone number
    assert quiet.sent[-1] == ("+919876543210", "654321")
