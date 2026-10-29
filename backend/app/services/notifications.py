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
            # WARNING because no logging config exists yet and INFO would be dropped. Last four
            # digits only (R5.2: no full phone numbers); the code is allowed for the mock in dev.
            logger.warning("MOCK SMS to ***%s: OTP code %s", phone[-4:], code)


def create_sms_provider(settings: Settings) -> SmsProvider:
    return MockSmsProvider(log_codes=settings.app_env in ("local", "test"))
