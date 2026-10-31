from typing import Any

import httpx
import jwt
import pytest
from sqlalchemy import select, update

from app.core.security import hash_password
from app.models import RefreshToken, User
from tests.integration.api import ORIGIN, Api, new_email, new_phone, open_api
from tests.integration.test_businesses import user_headers
from tests.integration.test_idempotency import create

pytestmark = pytest.mark.anyio

PASSWORD = "correct horse battery"


async def login_owner(api: Api, client: httpx.AsyncClient | None = None) -> httpx.Response:
    email = new_email()
    await api.create_user("owner", email=email, password_hash=hash_password(PASSWORD))
    return await (client or api.client).post(
        "/v1/auth/login", json={"email": email, "password": PASSWORD}
    )


def cookie_attributes(response: httpx.Response) -> dict[str, str]:
    """Parses the raw Set-Cookie header; the cookie jar hides attributes."""
    parts = [part.strip() for part in response.headers["set-cookie"].split(";")]
    name, _, value = parts[0].partition("=")
    attributes = {"name": name, "value": value}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        attributes[key.lower()] = val
    return attributes


def replay(old_token: str) -> dict[str, str]:
    return {"Cookie": f"refresh_token={old_token}"}


async def family_revoked(api: Api, login: httpx.Response) -> list[bool]:
    user_id = login.json()["user"]["id"]
    async with api.sessions() as session:
        rows = (
            await session.execute(
                select(RefreshToken.revoked_at).where(RefreshToken.user_id == user_id)
            )
        ).scalars()
        return [revoked is not None for revoked in rows]


async def test_refresh_rotates_the_cookie_and_returns_a_new_access_token(api: Api) -> None:
    login = await login_owner(api)
    old_cookie = api.client.cookies["refresh_token"]
    refreshed = await api.client.post("/v1/auth/refresh")
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert (
        set(body) == {"access_token", "user"} and body["user"]["id"] == login.json()["user"]["id"]
    )
    assert api.client.cookies["refresh_token"] != old_cookie
    assert api.client.cookies["refresh_token"] not in refreshed.text
    jwt.decode(body["access_token"], api.settings.jwt_secret, algorithms=["HS256"])
    assert (await api.client.post("/v1/auth/refresh")).status_code == 200  # the new cookie works


async def test_refresh_uses_the_role_in_the_database_not_the_old_token(api: Api) -> None:
    login = await login_owner(api)
    user_id = login.json()["user"]["id"]
    async with api.sessions() as session:
        await session.execute(update(User).where(User.id == user_id).values(role="staff"))
        await session.commit()
    refreshed = await api.client.post("/v1/auth/refresh")
    claims = jwt.decode(
        refreshed.json()["access_token"], api.settings.jwt_secret, algorithms=["HS256"]
    )
    assert claims["role"] == "staff" and refreshed.json()["user"]["role"] == "staff"


async def test_reusing_a_rotated_cookie_revokes_the_family_and_clears_the_cookie(api: Api) -> None:
    login = await login_owner(api)
    old = api.client.cookies["refresh_token"]
    await api.client.post("/v1/auth/refresh")
    async with api.new_client() as attacker:
        reused = await attacker.post("/v1/auth/refresh", headers=replay(old))
    assert reused.status_code == 401 and reused.json()["code"] == "UNAUTHENTICATED"
    cleared = cookie_attributes(reused)
    assert cleared["value"] in ('""', "") and cleared["max-age"] == "0"
    assert all(await family_revoked(api, login))
    assert (await api.client.post("/v1/auth/refresh")).status_code == 401  # legitimate holder too


async def test_missing_cookie_is_401_and_clears_the_cookie(api: Api) -> None:
    response = await api.client.post("/v1/auth/refresh")
    assert response.status_code == 401 and response.json()["code"] == "UNAUTHENTICATED"
    assert cookie_attributes(response)["max-age"] == "0"


@pytest.mark.parametrize(
    ("overrides", "secure", "domain"),
    [
        ({}, True, None),
        ({"app_env": "local"}, False, None),
        ({"cookie_domain": ".example.com"}, True, ".example.com"),
    ],
)
async def test_cookie_attributes_are_exact_and_deletion_repeats_them(
    migrated_url: str,
    redis_url: str,
    overrides: dict[str, object],
    secure: bool,
    domain: str | None,
) -> None:
    async with open_api(migrated_url, redis_url, overrides) as api:
        set_cookie = cookie_attributes(await login_owner(api))
        # A fresh jar is used for the replay so the deletion headers are the only ones inspected.
        api.client.cookies.clear()
        api.client.cookies.set("refresh_token", set_cookie["value"], domain="test", path="/v1/auth")
        logout = await api.client.post("/v1/auth/logout")
        missing = await api.new_client().post("/v1/auth/refresh")
    expected = {
        "name": "refresh_token",
        "httponly": "",
        "samesite": "lax",
        "path": "/v1/auth",
        "max-age": str(api.settings.refresh_ttl_days * 86400),
    }
    if secure:
        expected["secure"] = ""
    if domain:
        expected["domain"] = domain
    assert {k: v for k, v in set_cookie.items() if k != "value"} == expected
    deletion = {k: v for k, v in expected.items() if k != "max-age"} | {"max-age": "0"}
    for response in (logout, missing):
        attributes = cookie_attributes(response)
        assert {k: v for k, v in attributes.items() if k not in ("value", "expires")} == deletion


async def test_logout_revokes_the_whole_family_and_is_idempotent(api: Api) -> None:
    login = await login_owner(api)
    await api.client.post("/v1/auth/refresh")  # two rows in the family
    old = api.client.cookies["refresh_token"]
    assert (await api.client.post("/v1/auth/logout")).status_code == 204
    assert all(await family_revoked(api, login))
    async with api.new_client() as other:
        assert (await other.post("/v1/auth/refresh", headers=replay(old))).status_code == 401
        assert (await other.post("/v1/auth/logout", headers=replay(old))).status_code == 204
        assert (await other.post("/v1/auth/logout")).status_code == 204
        assert (
            await other.post("/v1/auth/logout", headers=replay("never-issued"))
        ).status_code == 204


async def test_logout_leaves_other_devices_signed_in(api: Api) -> None:
    await login_owner(api)
    async with api.new_client() as second:
        await login_owner(api, second)
        await api.client.post("/v1/auth/logout")
        assert (await second.post("/v1/auth/refresh")).status_code == 200


@pytest.mark.parametrize("path", ["/v1/auth/refresh", "/v1/auth/logout"])
async def test_cookie_posts_reject_a_foreign_origin_but_allow_no_origin(
    api: Api, path: str
) -> None:
    login = await login_owner(api)
    foreign = await api.client.post(path, headers={"Origin": "http://evil.test"})
    assert foreign.status_code == 403 and foreign.json()["code"] == "FORBIDDEN"
    assert all(not revoked for revoked in await family_revoked(api, login))  # nothing was consumed
    assert (await api.client.post(path, headers={"Origin": "null"})).status_code == 403
    allowed = await api.client.post(path, headers={"Origin": ORIGIN})
    assert allowed.status_code in (200, 204)


async def test_cookie_post_without_origin_is_allowed(api: Api) -> None:
    await login_owner(api)
    assert (await api.client.post("/v1/auth/refresh")).status_code == 200


async def test_preflight_allows_only_the_configured_origin_and_headers(api: Api) -> None:
    headers = "authorization,content-type,idempotency-key,last-event-id"
    allowed = await api.client.options(
        "/v1/auth/login",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": headers,
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert allowed.headers["access-control-allow-credentials"] == "true"
    allow_headers = {
        h.strip().lower() for h in allowed.headers["access-control-allow-headers"].split(",")
    }
    assert set(headers.split(",")) <= allow_headers
    methods = {m.strip() for m in allowed.headers["access-control-allow-methods"].split(",")}
    assert methods == {"GET", "POST", "PATCH", "DELETE"}

    foreign = await api.client.options(
        "/v1/auth/login",
        headers={"Origin": "http://evil.test", "Access-Control-Request-Method": "POST"},
    )
    assert foreign.status_code == 400 and "access-control-allow-origin" not in foreign.headers

    bad_header = await api.client.options(
        "/v1/auth/login",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-evil",
        },
    )
    assert bad_header.status_code == 400

    put = await api.client.options(
        "/v1/auth/login",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "PUT"},
    )
    assert put.status_code == 400


async def test_actual_responses_carry_cors_headers_only_for_the_allowed_origin(api: Api) -> None:
    allowed = await api.client.get("/healthz", headers={"Origin": ORIGIN})
    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert allowed.headers["access-control-allow-credentials"] == "true"
    foreign = await api.client.get("/healthz", headers={"Origin": "http://evil.test"})
    assert "access-control-allow-origin" not in foreign.headers
    assert "access-control-allow-origin" not in (await api.client.get("/healthz")).headers


def exposed(response: Any) -> set[str]:
    raw = response.headers.get("access-control-expose-headers", "")
    return {name.strip().lower() for name in raw.split(",") if name.strip()}


async def test_cors_exposes_retry_after_and_the_replay_header(api: Api) -> None:
    phone = new_phone()
    assert (await api.request_otp(phone)).status_code == 202
    cooldown = await api.client.post(
        "/v1/auth/otp/request", json={"phone": phone}, headers={"Origin": ORIGIN}
    )
    assert cooldown.status_code == 429 and "retry-after" in cooldown.headers
    assert cooldown.headers["access-control-allow-origin"] == ORIGIN
    assert exposed(cooldown) == {"retry-after", "idempotent-replayed"}
    _, owner = await user_headers(api, "owner")
    replayed = None
    for _ in range(2):
        replayed = await create(api, owner | {"Origin": ORIGIN})
    assert replayed is not None and replayed.headers["Idempotent-Replayed"] == "true"
    assert exposed(replayed) == {"retry-after", "idempotent-replayed"}
    foreign = await api.client.post(
        "/v1/auth/otp/request", json={"phone": phone}, headers={"Origin": "http://evil.test"}
    )
    assert foreign.status_code == 429
    # Starlette adds its fixed CORS headers to every response; without Allow-Origin the browser
    # gives a foreign page nothing of the response, these headers included.
    assert "access-control-allow-origin" not in foreign.headers
