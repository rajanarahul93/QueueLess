"""Masks OTP codes, phone numbers and email addresses in log output.

Used where codes are NOT meant to be printed (demo and production). Local and test runs keep the
mock provider's code line on purpose: the end-to-end suite reads the sign-in code from it.
"""

import logging
import re

OTP = re.compile(r"(?i)\b(otp|code|passcode)\b([^0-9\n]{0,12})\d{4,8}\b")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# A number with a plus sign, or ten digits starting 6-9 (an Indian mobile). Ordinary numbers,
# timestamps and ids stay readable; epoch seconds start with 1 and are not matched.
PHONE = re.compile(r"\+\d{1,3}[\d ()-]{7,14}\d|(?<![\d.])[6-9]\d{9}(?![\d.])")


def scrub(text: str) -> str:
    text = OTP.sub(lambda match: f"{match.group(1)}{match.group(2)}[otp]", text)
    text = EMAIL.sub("[email]", text)
    return PHONE.sub("[phone]", text)


class ScrubFilter(logging.Filter):
    """Masks the formatted message (so arguments too) and any exception text on the record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True  # a bad format string: the handler reports it, there is nothing to mask
        record.msg = scrub(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub(record.stack_info)
        return True


def install_log_scrub() -> None:
    """Adds the filter to every handler that exists now, on the root and on every named logger,
    which includes uvicorn's access log and arq's. A handler gets it once."""
    loggers = [logging.getLogger()]
    loggers += [
        found
        for found in logging.root.manager.loggerDict.values()
        if isinstance(found, logging.Logger)
    ]
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(existing, ScrubFilter) for existing in handler.filters):
                handler.addFilter(ScrubFilter())
