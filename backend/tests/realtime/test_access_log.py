"""Stream tokens travel in the query string; with the app's INFO lines on, none reaches a log."""

import io
import logging
import secrets
import uuid

import httpx
import pytest

from app.realtime.sse import RedactStreamToken
from tests.integration.api import Api
from tests.realtime.conftest import serve

pytestmark = pytest.mark.anyio


async def test_a_stream_request_logs_no_stream_token_value(api: Api) -> None:
    access_log = logging.getLogger("uvicorn.access")
    kept = [f for f in access_log.filters if isinstance(f, RedactStreamToken)]
    for redaction in kept:  # the app created below must add it again itself
        access_log.removeFilter(redaction)
    level = access_log.level
    out = io.StringIO()
    capture = logging.StreamHandler(out)
    loggers = [access_log, logging.getLogger("uvicorn.error"), logging.getLogger()]
    marker = secrets.token_urlsafe(24)
    try:
        async with serve(api) as (url, _):
            access_log.setLevel(logging.INFO)  # serve() runs uvicorn at warning
            for logger in loggers:
                logger.addHandler(capture)
            async with httpx.AsyncClient(base_url=url) as http:
                response = await http.get(f"/v1/queues/{uuid.uuid4()}/stream?stream_token={marker}")
            assert response.status_code == 401
    finally:
        for logger in loggers:
            logger.removeHandler(capture)
        access_log.setLevel(level)
        for redaction in kept:
            if not any(isinstance(f, RedactStreamToken) for f in access_log.filters):
                access_log.addFilter(redaction)
    text = out.getvalue()
    assert "stream_token=[redacted]" in text and marker not in text
