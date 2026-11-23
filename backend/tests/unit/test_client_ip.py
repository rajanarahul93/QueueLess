"""CLIENT_IP_HEADER: unset keeps the peer address; set, a valid IP in that one header wins."""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import ValidationError

from app.core.rate_limit import client_ip
from tests.helpers import make_settings

pytestmark = pytest.mark.anyio

PEER = "198.51.100.7"
CLIENT = "203.0.113.9"


def make_app(header: str = "") -> FastAPI:
    app = FastAPI()
    app.state.settings = make_settings(client_ip_header=header)

    @app.get("/ip")
    async def ip(request: Request) -> str:
        return client_ip(request)

    return app


async def seen(header: str, sent: dict[str, str]) -> str:
    transport = httpx.ASGITransport(app=make_app(header), client=(PEER, 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ip", headers=sent)
    assert response.status_code == 200, response.text
    return str(response.json())


async def test_unset_is_the_peer_address_whatever_headers_arrive() -> None:
    sent = {"x-real-ip": CLIENT, "x-forwarded-for": "192.0.2.1", "forwarded": "for=192.0.2.2"}
    assert await seen("", sent) == PEER


def test_without_a_peer_the_address_is_unknown() -> None:
    scope: dict[str, Any] = {"type": "http", "headers": [], "client": None, "app": make_app()}
    assert client_ip(Request(scope)) == "unknown"
    scope["app"] = make_app("x-real-ip")
    assert client_ip(Request(scope)) == "unknown"  # and a missing header changes nothing


async def test_set_and_valid_uses_that_header() -> None:
    assert await seen("x-real-ip", {"x-real-ip": CLIENT}) == CLIENT


async def test_the_header_name_is_case_insensitive_both_ways() -> None:
    assert await seen("X-Real-IP", {"x-real-ip": CLIENT}) == CLIENT
    assert await seen("x-real-ip", {"X-REAL-IP": CLIENT}) == CLIENT


async def test_an_ipv6_address_is_normalised() -> None:
    assert await seen("x-real-ip", {"x-real-ip": "2001:DB8:0:0:0:0:0:1"}) == "2001:db8::1"


@pytest.mark.parametrize(
    "value",
    ["", "not-an-ip", "203.0.113.999", "203.0.113.9, 10.0.0.1", "203.0.113.9:8080", " ", "1.2.3"],
)
async def test_set_and_invalid_falls_back_to_the_peer(value: str) -> None:
    assert await seen("x-real-ip", {"x-real-ip": value}) == PEER


async def test_set_and_missing_falls_back_to_the_peer() -> None:
    assert await seen("x-real-ip", {}) == PEER


async def test_spoof_attempts_with_extra_headers_change_nothing() -> None:
    spoof = {
        "x-forwarded-for": "192.0.2.1",
        "forwarded": "for=192.0.2.2",
        "x-client-ip": "192.0.2.3",
    }
    assert await seen("x-real-ip", spoof) == PEER  # the configured header is absent
    assert await seen("x-real-ip", spoof | {"x-real-ip": CLIENT}) == CLIENT  # and it alone is read


@pytest.mark.parametrize("name", ["x-forwarded-for", "X-Forwarded-For"])
def test_x_forwarded_for_cannot_be_the_configured_header(name: str) -> None:
    with pytest.raises(ValidationError, match="client_ip_header"):
        make_settings(client_ip_header=name)


@pytest.mark.parametrize("name", ["bad header", "x_real_ip!", "x-real-ip\n", "a" * 100])
def test_a_header_name_must_be_a_plain_http_name(name: str) -> None:
    with pytest.raises(ValidationError, match="client_ip_header"):
        make_settings(client_ip_header=name)


def test_the_default_is_unset() -> None:
    assert make_settings().client_ip_header == ""
