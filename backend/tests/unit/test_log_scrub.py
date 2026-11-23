"""The log filter masks OTP codes, phone numbers and emails; everything else stays readable."""

import io
import logging
from collections.abc import Iterator

import pytest

from app.core.config import SCRUBBED_ENVS
from app.core.log_scrub import ScrubFilter, install_log_scrub, scrub
from app.main import create_app
from tests.helpers import make_settings

PHONE = "+919876543210"
EMAIL = "asha.k+queue@example.test"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("MOCK SMS to ***3210: OTP code 576990", "MOCK SMS to ***3210: OTP code [otp]"),
        ("otp=123456 requested", "otp=[otp] requested"),
        ('{"phone": "+919876543210", "code": "123456"}', '{"phone": "[phone]", "code": "[otp]"}'),
        ("verify failed for 9876543210 code 654321", "verify failed for [phone] code [otp]"),
        ("sent to +1 (415) 555-0100 now", "sent to [phone] now"),
        ("sent to +14155550100", "sent to [phone]"),
        (f"customer {EMAIL} rescheduled", "customer [email] rescheduled"),
        ("owner=a.b@sub.example.co.uk login", "owner=[email] login"),
    ],
)
def test_scrub_masks_the_sensitive_parts(raw: str, expected: str) -> None:
    assert scrub(raw) == expected
    assert PHONE not in scrub(raw) and "576990" not in scrub(raw)


@pytest.mark.parametrize(
    "line",
    [
        "2026-11-22 17:15:27,537 WARNING app.services.queue eta cache unavailable",
        "relay pass failed error=OSError; retrying",
        "refresh reuse within grace window family_id=0f8fad5b-d9cb-469f-a165-70867728950e",
        "retention {'deleted': 12, 'seen': 4500}",
        "stream_token=[redacted] 200 OK",
        "token #5 called at 1763834400",
        "listening on 127.0.0.1:8000",
        "POST /v1/auth/otp/request 202 in 14 ms",
        "slot 2026-11-22T10:30:00+05:30 booked",
    ],
)
def test_scrub_leaves_ordinary_lines_alone(line: str) -> None:
    assert scrub(line) == line


def test_scrub_is_idempotent() -> None:
    once = scrub(f"{PHONE} {EMAIL} OTP code 123456")
    assert scrub(once) == once


@pytest.fixture
def captured() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(ScrubFilter())
    logger = logging.getLogger("app.scrub_test")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    yield logger, out
    logger.removeHandler(handler)


def test_the_filter_masks_the_message_and_its_arguments(
    captured: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, out = captured
    logger.warning("sent to %s: code %s for %s", PHONE, "123456", EMAIL)
    logger.info("plain %d", 7)
    text = out.getvalue()
    assert "sent to [phone]: code [otp] for [email]" in text
    assert "plain 7" in text
    assert PHONE not in text and "123456" not in text and EMAIL not in text


def test_the_filter_masks_exception_text(captured: tuple[logging.Logger, io.StringIO]) -> None:
    logger, out = captured
    try:
        raise RuntimeError(f"could not text {PHONE} about {EMAIL}")
    except RuntimeError as error:
        logger.error("sms failed", exc_info=error)
    text = out.getvalue()
    assert "RuntimeError" in text and "[phone]" in text and "[email]" in text
    assert PHONE not in text and EMAIL not in text


def test_a_message_that_cannot_be_formatted_is_left_alone() -> None:
    record = logging.LogRecord(
        "app.x", logging.WARNING, "f.py", 1, "needs two %s %s", ("one",), None
    )
    assert ScrubFilter().filter(record) is True  # the handler reports the bad format itself
    assert record.msg == "needs two %s %s" and record.args == ("one",)


@pytest.fixture
def uvicorn_access() -> Iterator[io.StringIO]:
    """A handler on uvicorn.access, as uvicorn installs one before the app is created."""
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    logger = logging.getLogger("uvicorn.access")
    level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    yield out
    logger.removeHandler(handler)
    logger.setLevel(level)
    for existing in logging.getLogger().handlers + logger.handlers:
        existing.filters = [f for f in existing.filters if not isinstance(f, ScrubFilter)]


def test_installing_the_scrub_covers_the_access_log(uvicorn_access: io.StringIO) -> None:
    install_log_scrub()
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", f"/v1/x?email={EMAIL}", "1.1", 200
    )
    text = uvicorn_access.getvalue()
    assert "email=[email]" in text and EMAIL not in text


def test_installing_twice_adds_one_filter_per_handler(uvicorn_access: io.StringIO) -> None:
    install_log_scrub()
    install_log_scrub()
    handlers = logging.getLogger("uvicorn.access").handlers
    assert handlers and all(
        sum(isinstance(f, ScrubFilter) for f in h.filters) == 1 for h in handlers
    )


def test_the_scrubbed_environments_are_demo_and_production() -> None:
    assert SCRUBBED_ENVS == ("demo", "production")


def test_the_app_installs_the_scrub_in_demo(uvicorn_access: io.StringIO) -> None:
    # Production is in SCRUBBED_ENVS too, but its settings cannot be built until a real OTP
    # provider exists, so demo is the one scrubbed environment an app can start in today.
    create_app(make_settings(app_env="demo", otp_provider="demo", cors_origins=["https://a.test"]))
    logging.getLogger("uvicorn.access").info("sign-in for %s", EMAIL)
    assert EMAIL not in uvicorn_access.getvalue()


@pytest.mark.parametrize("app_env", ["local", "test"])
def test_the_app_leaves_local_and_test_logs_unmasked(
    uvicorn_access: io.StringIO, app_env: str
) -> None:
    # The mock provider prints the sign-in code on purpose here; the e2e suite reads it.
    create_app(make_settings(app_env=app_env))
    logging.getLogger("uvicorn.access").info("MOCK SMS to ***3210: OTP code %s", "123456")
    assert "123456" in uvicorn_access.getvalue()
