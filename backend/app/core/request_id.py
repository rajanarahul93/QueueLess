"""A request id on every response and in every log record written while the request runs.

The id comes from the caller only when it is short and plain; anything else is replaced, so a
header can never put odd characters or a forged line into the logs.
"""

import logging
import re
import uuid
from contextvars import ContextVar

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "x-request-id"
SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
NO_REQUEST = "-"

_current: ContextVar[str] = ContextVar("request_id", default=NO_REQUEST)


def accept_request_id(value: str | None) -> str:
    """The caller's id when it matches the safe pattern, else a new uuid4."""
    if value is not None and SAFE_REQUEST_ID.fullmatch(value):
        return value
    return str(uuid.uuid4())


def request_id_of(request: Request) -> str:
    """The id this request runs under; "-" when no middleware assigned one (the app has it)."""
    assigned = request.scope.get("request_id")
    if isinstance(assigned, str):
        return assigned
    inbound = request.headers.get(REQUEST_ID_HEADER)
    return inbound if inbound is not None and SAFE_REQUEST_ID.fullmatch(inbound) else NO_REQUEST


class RequestIdFilter(logging.Filter):
    """Puts the running request's id on each record as `request_id` ("-" outside a request)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _current.get()
        return True


class RequestIdFormatter(logging.Formatter):
    """The usual line, plus ` request_id=<id>` on its first line when a request is running.
    Lines written outside a request, and lines that already name the id, are left as they were."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        request_id = getattr(record, "request_id", NO_REQUEST)
        first, newline, rest = text.partition("\n")
        if request_id == NO_REQUEST or "request_id=" in first:
            return text
        return f"{first} request_id={request_id}{newline}{rest}"


class RequestIdMiddleware:
    """Plain ASGI, not BaseHTTPMiddleware, so server-sent-event bodies still stream untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        inbound = next(
            (
                value.decode("latin-1")
                for name, value in scope["headers"]
                if name == b"x-request-id"
            ),
            None,
        )
        request_id = accept_request_id(inbound)
        # The unhandled-error handler runs outside this middleware and reads the id from here.
        scope["request_id"] = request_id
        token = _current.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _current.reset(token)
