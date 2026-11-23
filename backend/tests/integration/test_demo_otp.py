"""Demo mode (APP_ENV=demo, OTP_PROVIDER=demo): the 202 body shows the code, nothing else moves."""

import logging
import re
from collections.abc import AsyncIterator

import pytest

from app.services.notifications import DemoSmsProvider, MockSmsProvider
from tests.integration.api import Api, new_phone, open_api
from tests.integration.test_auth_otp import COOLDOWN, redis_contents

pytestmark = pytest.mark.anyio

DEMO: dict[str, object] = {"app_env": "demo", "otp_provider": "demo"}


@pytest.fixture
async def demo(migrated_url: str, redis_url: str) -> AsyncIterator[Api]:
    async with open_api(migrated_url, redis_url, DEMO) as opened:
        yield opened


async def shown_code(api: Api, phone: str) -> str:
    response = await api.request_otp(phone)
    assert response.status_code == 202, response.text
    assert set(response.json()) == {"demo_code"}
    code = str(response.json()["demo_code"])
    assert re.fullmatch(r"\d{6}", code), code
    return code


# --- what the request shows ---


async def test_the_request_shows_a_six_digit_code_that_signs_in(demo: Api) -> None:
    phone = new_phone()
    code = await shown_code(demo, phone)
    response = await demo.verify_otp(phone, code, name="Demo Customer")
    assert response.status_code == 200, response.text
    assert response.json()["access_token"]


async def test_demo_cookies_are_secure(demo: Api) -> None:
    phone = new_phone()
    response = await demo.verify_otp(phone, await shown_code(demo, phone))
    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie


async def test_each_request_gets_a_fresh_code_and_the_old_one_stops_working(demo: Api) -> None:
    phone = new_phone()
    first = await shown_code(demo, phone)
    demo.clock.now += COOLDOWN
    second = await shown_code(demo, phone)
    if first != second:  # a repeat is possible once in a million; then there is nothing to prove
        assert (await demo.verify_otp(phone, first)).json()["code"] == "OTP_INVALID"
    assert (await demo.verify_otp(phone, second)).status_code == 200


# --- what stays hidden ---


async def test_the_provider_sends_nothing_and_keeps_no_codes(demo: Api) -> None:
    assert isinstance(demo.app.state.sms, DemoSmsProvider)
    assert not isinstance(demo.app.state.sms, MockSmsProvider)
    assert not hasattr(demo.app.state.sms, "sent")


async def test_the_code_is_never_logged(demo: Api, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    phone = new_phone()
    code = await shown_code(demo, phone)
    await demo.verify_otp(phone, code)
    assert code not in caplog.text
    assert "OTP code" not in caplog.text


async def test_the_demo_provider_itself_logs_nothing() -> None:
    # Without the scrub filter in the way, which demo mode installs on every handler: whatever the
    # provider logs would show up here, code included.
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    root = logging.getLogger()
    handler = Collect(logging.DEBUG)
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        await DemoSmsProvider().send_otp(new_phone(), "123456")
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
    assert records == []


async def test_redis_still_holds_only_the_hash(demo: Api) -> None:
    phone = new_phone()
    code = await shown_code(demo, phone)
    held = b" ".join(await redis_contents(demo.redis))
    assert code.encode() not in held and phone.encode() not in held


async def test_only_the_success_response_carries_the_code(demo: Api) -> None:
    phone = new_phone()
    await shown_code(demo, phone)
    denied = await demo.request_otp(phone)  # inside the resend cooldown
    assert denied.status_code == 429
    assert "demo_code" not in denied.text
    invalid = await demo.request_otp("not-a-phone")
    assert invalid.status_code == 422 and "demo_code" not in invalid.text


# --- every limit stays ---


async def test_resend_cooldown_stays(demo: Api) -> None:
    phone = new_phone()
    await shown_code(demo, phone)
    denied = await demo.request_otp(phone)
    assert denied.status_code == 429 and denied.json()["code"] == "RATE_LIMITED"
    demo.clock.now += COOLDOWN
    assert (await demo.request_otp(phone)).status_code == 202


async def test_per_phone_limit_stays_five_per_hour(demo: Api) -> None:
    phone = new_phone()
    for _ in range(5):
        await shown_code(demo, phone)
        demo.clock.now += COOLDOWN
    denied = await demo.request_otp(phone)
    assert denied.status_code == 429 and "demo_code" not in denied.text


async def test_per_ip_limit_stays_twenty_per_hour(demo: Api) -> None:
    for _ in range(20):
        await shown_code(demo, new_phone())
    denied = await demo.request_otp(new_phone())
    assert denied.status_code == 429 and "demo_code" not in denied.text


async def test_attempt_lockout_and_expiry_stay(demo: Api) -> None:
    phone = new_phone()
    code = await shown_code(demo, phone)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(demo.settings.otp_max_attempts):
        assert (await demo.verify_otp(phone, wrong)).json()["code"] == "OTP_INVALID"
    locked = await demo.verify_otp(phone, code)  # even the right code is refused now
    assert locked.status_code == 429

    other = new_phone()
    late = await shown_code(demo, other)
    demo.clock.now += demo.settings.otp_ttl_sec + 1
    expired = await demo.verify_otp(other, late)
    assert expired.status_code == 400 and expired.json()["code"] == "OTP_EXPIRED"


async def test_a_code_works_once(demo: Api) -> None:
    phone = new_phone()
    code = await shown_code(demo, phone)
    assert (await demo.verify_otp(phone, code)).status_code == 200
    again = await demo.verify_otp(phone, code)
    assert again.status_code == 400 and again.json()["code"] == "OTP_EXPIRED"


# --- every other configuration is exactly as it was ---


@pytest.mark.parametrize("app_env", ["local", "test"])
async def test_the_mock_request_body_is_byte_identical(
    migrated_url: str, redis_url: str, app_env: str
) -> None:
    async with open_api(migrated_url, redis_url, {"app_env": app_env}) as api:
        response = await api.request_otp(new_phone())
        assert response.status_code == 202
        assert response.content == b"{}"
        assert isinstance(api.app.state.sms, MockSmsProvider)
