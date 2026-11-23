import logging
from collections import deque
from typing import Protocol

from app.core.config import Settings

logger = logging.getLogger(__name__)


class SmsProvider(Protocol):
    """The one place a real SMS provider plugs in (PRD D16)."""

    async def send_otp(self, phone: str, code: str) -> None: ...


class MockSmsProvider:
    def __init__(self, log_codes: bool) -> None:
        self.sent: deque[tuple[str, str]] = deque(maxlen=100)  # (phone, code), for tests
        self._log_codes = log_codes

    async def send_otp(self, phone: str, code: str) -> None:
        self.sent.append((phone, code))
        if self._log_codes:
            # WARNING so it still shows with LOG_LEVEL=WARNING. Last four digits only (R5.2: no
            # full phone numbers); the code is allowed for the mock in dev.
            logger.warning("MOCK SMS to ***%s: OTP code %s", phone[-4:], code)


class DemoSmsProvider:
    """Demo mode: no message is sent and nothing is kept or logged. The request endpoint shows the
    code to the caller instead (see `auth_service.request_otp`); the settings only allow this
    provider with APP_ENV=demo."""

    async def send_otp(self, phone: str, code: str) -> None:
        return None


def create_sms_provider(settings: Settings) -> SmsProvider:
    if settings.otp_provider == "demo":
        return DemoSmsProvider()
    return MockSmsProvider(log_codes=settings.app_env in ("local", "test"))
