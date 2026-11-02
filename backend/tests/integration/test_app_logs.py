"""The API prints the app's own log lines (app.*), and none of them carries personal data."""

import io
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from app.core.config import APP_LOG_HANDLER
from app.db.session import create_engine
from app.main import create_app
from app.services.notifications import MockSmsProvider
from tests.helpers import make_settings
from tests.integration.api import Api, new_phone
from tests.integration.test_auth_session import PASSWORD, login_owner, replay
from tests.integration.test_booking import local, world

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def keep_app_level() -> Iterator[None]:
    level = logging.getLogger("app").level
    yield
    logging.getLogger("app").setLevel(level)


def configured() -> logging.StreamHandler[io.TextIOBase]:
    app_logger = logging.getLogger("app")
    [handler] = [h for h in app_logger.handlers if h.get_name() == APP_LOG_HANDLER]
    assert isinstance(handler, logging.StreamHandler)
    return handler


def drop_configured() -> None:
    """So a test proves its own startup installs the handler, not an earlier one."""
    app_logger = logging.getLogger("app")
    for handler in [h for h in app_logger.handlers if h.get_name() == APP_LOG_HANDLER]:
        app_logger.removeHandler(handler)


@contextmanager
def printed(include_root: bool = False) -> Iterator[io.StringIO]:
    """What the configured handler prints; with include_root, also anything reaching the root."""
    out = io.StringIO()
    handler = configured()
    previous = handler.setStream(out)
    root = logging.StreamHandler(out)
    if include_root:
        logging.getLogger().addHandler(root)
    try:
        yield out
    finally:
        handler.setStream(previous or out)
        logging.getLogger().removeHandler(root)


async def test_the_api_factory_prints_app_info_lines_through_the_configured_handler() -> None:
    drop_configured()
    create_app(make_settings())
    assert logging.getLogger("app").level == logging.INFO
    with printed() as out:
        logging.getLogger("app.services.auth").info("probe family_id=%s", "f1")
    assert out.getvalue().endswith(" INFO app.services.auth probe family_id=f1\n")


async def test_level_warning_hides_info_lines() -> None:
    create_app(make_settings(log_level="warning"))
    with printed() as out:
        logging.getLogger("app.services.auth").info("hidden line")
        logging.getLogger("app.services.auth").warning("shown line")
    assert "hidden line" not in out.getvalue() and "shown line" in out.getvalue()


async def test_the_worker_startup_configures_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", make_settings().redis_url)  # read when the module loads
    from app.workers import main as worker

    class Configured(Exception):
        pass

    levels: list[str] = []

    def record(level: str) -> None:
        levels.append(level)
        raise Configured  # stop before the worker opens connections

    monkeypatch.setattr(worker, "configure_logging", record)
    with pytest.raises(Configured):
        await worker.startup({"settings": make_settings(log_level="warning")})
    assert levels == ["WARNING"]


async def test_database_errors_are_logged_without_bound_values() -> None:
    engine = create_engine(make_settings().database_url)
    assert engine.sync_engine.hide_parameters
    await engine.dispose()


async def test_otp_login_booking_and_grace_refresh_log_no_personal_data(api: Api) -> None:
    api.app.state.sms = MockSmsProvider(log_codes=False)  # outside local and test, no codes
    with printed(include_root=True) as out:
        phone = new_phone()
        assert (await api.request_otp(phone)).status_code == 202
        code = api.last_code()
        verified = await api.verify_otp(phone, code, name="Asha Customer")
        assert verified.status_code == 200
        customer_cookie = api.client.cookies["refresh_token"]
        customer_access = verified.json()["access_token"]
        booked = await (await world(api)).book(
            {"Authorization": f"Bearer {customer_access}"}, local(9)
        )
        assert booked.status_code == 201, booked.text
        login = await login_owner(api)
        old = api.client.cookies["refresh_token"]
        await api.client.post("/v1/auth/refresh")
        successor = api.client.cookies["refresh_token"]
        api.clock.now += 2
        async with api.new_client() as retry:
            reused = await retry.post("/v1/auth/refresh", headers=replay(old))
        assert reused.status_code == 200
    text = out.getvalue()
    assert " INFO app.services.auth refresh reuse within grace window family_id=" in text
    for value in (
        phone[:-4],
        phone[1:],
        code,
        "Asha Customer",
        login.json()["user"]["email"],
        PASSWORD,
        customer_cookie,
        customer_access,
        login.json()["access_token"],
        old,
        successor,
        reused.json()["access_token"],
    ):
        assert value not in text
