import asyncio
import json
import uuid

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select

from app.core import idempotency
from app.core.errors import ServiceUnavailable
from app.core.idempotency import body_fingerprint, claim_key
from app.models import Business, User
from app.services import business as business_service
from tests.integration.api import Api, new_email
from tests.integration.test_business_admin import PASSWORD
from tests.integration.test_businesses import (
    SERVICE,
    business_body,
    new_business,
    user_headers,
)

pytestmark = pytest.mark.anyio

KEY = "retry-1"


def keyed(headers: dict[str, str], key: str = KEY) -> dict[str, str]:
    return headers | {"Idempotency-Key": key}


async def business_count(api: Api, owner: User) -> int:
    async with api.sessions() as session:
        count = await session.scalar(
            select(func.count()).select_from(Business).where(Business.owner_id == owner.id)
        )
        return int(count or 0)


async def create(api: Api, headers: dict[str, str], key: str = KEY, **body: object):  # type: ignore[no-untyped-def]
    return await api.client.post(
        "/v1/businesses", json=business_body(**body), headers=keyed(headers, key)
    )


async def test_same_key_and_body_replays_without_creating_again(api: Api) -> None:
    owner, headers = await user_headers(api, "owner")
    first = await create(api, headers)
    second = await create(api, headers)
    assert first.status_code == second.status_code == 201
    assert "Idempotent-Replayed" not in first.headers
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json() == first.json()
    assert await business_count(api, owner) == 1


async def test_without_a_key_nothing_changes(api: Api) -> None:
    owner, headers = await user_headers(api, "owner")
    for _ in range(2):
        response = await api.client.post("/v1/businesses", json=business_body(), headers=headers)
        assert response.status_code == 201 and "Idempotent-Replayed" not in response.headers
    assert await business_count(api, owner) == 2


async def test_different_body_with_the_same_key_is_a_conflict(api: Api) -> None:
    owner, headers = await user_headers(api, "owner")
    await create(api, headers)
    conflict = await create(api, headers, name="Another")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert "in_progress" not in conflict.json()
    assert await business_count(api, owner) == 1


async def test_key_order_and_whitespace_do_not_change_the_fingerprint(api: Api) -> None:
    owner, headers = await user_headers(api, "owner")
    body = business_body()
    one = await api.client.post(
        "/v1/businesses",
        content=json.dumps(body),
        headers=keyed(headers) | {"Content-Type": "application/json"},
    )
    reordered = json.dumps(dict(reversed(list(body.items()))), indent=2)
    two = await api.client.post(
        "/v1/businesses",
        content=reordered,
        headers=keyed(headers) | {"Content-Type": "application/json"},
    )
    assert two.headers["Idempotent-Replayed"] == "true" and two.json() == one.json()
    assert await business_count(api, owner) == 1


async def test_scope_is_per_user_route_and_path(api: Api) -> None:
    owner_a, headers_a = await user_headers(api, "owner")
    owner_b, headers_b = await user_headers(api, "owner")
    first = await create(api, headers_a)
    other_user = await create(api, headers_b)
    assert other_user.status_code == 201 and "Idempotent-Replayed" not in other_user.headers
    assert other_user.json()["id"] != first.json()["id"]
    business_one = uuid.UUID(first.json()["id"])
    business_two = await new_business(api, headers_a)
    for business_id in (business_one, business_two):  # same key, same route template, two paths
        response = await api.client.post(
            f"/v1/businesses/{business_id}/services", json=SERVICE, headers=keyed(headers_a)
        )
        assert response.status_code == 201 and "Idempotent-Replayed" not in response.headers
    assert owner_a and owner_b


async def test_concurrent_duplicates_run_the_handler_once(api: Api) -> None:
    owner, headers = await user_headers(api, "owner")
    responses = await asyncio.gather(*(create(api, headers) for _ in range(6)))
    assert await business_count(api, owner) == 1
    originals = [
        r for r in responses if r.status_code == 201 and "Idempotent-Replayed" not in r.headers
    ]
    assert len(originals) == 1
    for response in responses:
        assert response.status_code in (201, 409)
        if response.status_code == 409:
            assert response.json()["in_progress"] is True
            assert response.headers["Retry-After"] == "1"


async def seed_pending(api: Api, user: User, path: str, body: dict[str, object], key: str) -> None:
    settings = api.settings
    raw = json.dumps(body).encode()
    redis_key = claim_key(settings, user.id, "POST", path, key)
    await api.redis.set(redis_key, f"p:other:{body_fingerprint(settings, raw)}", px=60_000)


async def test_in_progress_claim_answers_409_with_retry_after(api: Api) -> None:
    owner, headers = await user_headers(api, "owner")
    await seed_pending(api, owner, "/v1/businesses", business_body(), KEY)
    same = await create(api, headers)
    assert same.status_code == 409 and same.json()["in_progress"] is True
    assert same.headers["Retry-After"] == "1"
    different = await create(api, headers, name="Another")
    assert different.status_code == 409 and "in_progress" not in different.json()
    assert await business_count(api, owner) == 0


async def test_invalid_body_does_not_consume_the_key(api: Api) -> None:
    owner, headers = await user_headers(api, "owner")
    bad = await api.client.post("/v1/businesses", json={}, headers=keyed(headers))
    assert bad.status_code == 422
    assert await api.redis.keys("idem:*") == []
    fixed = await create(api, headers)
    assert fixed.status_code == 201 and "Idempotent-Replayed" not in fixed.headers


async def test_non_json_body_is_hashed_raw_and_released(api: Api) -> None:
    _, headers = await user_headers(api, "owner")
    response = await api.client.post(
        "/v1/businesses",
        content=b"not json",
        headers=keyed(headers) | {"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert await api.redis.keys("idem:*") == []


async def test_error_responses_are_not_stored(api: Api) -> None:
    _, headers = await user_headers(api, "owner")
    business_id = await new_business(api, headers)
    url = f"/v1/businesses/{business_id}/services/{uuid.uuid4()}"
    for _ in range(2):
        response = await api.client.patch(url, json={"price": "1"}, headers=keyed(headers))
        assert response.status_code == 404 and "Idempotent-Replayed" not in response.headers
    assert await api.redis.keys("idem:*") == []


async def test_a_503_releases_the_claim_so_the_retry_runs(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, headers = await user_headers(api, "owner")
    real = business_service.create_business

    async def failing(*args: object, **kwargs: object) -> Business:
        raise ServiceUnavailable("down")

    monkeypatch.setattr(business_service, "create_business", failing)
    assert (await create(api, headers)).status_code == 503
    assert await api.redis.keys("idem:*") == []
    monkeypatch.setattr(business_service, "create_business", real)
    retry = await create(api, headers)
    assert retry.status_code == 201 and await business_count(api, owner) == 1


async def test_a_429_is_not_stored(api: Api) -> None:
    _, headers = await user_headers(api, "owner")
    for _ in range(10):
        await api.client.post("/v1/businesses", json=business_body(), headers=headers)
    blocked = await create(api, headers)
    assert blocked.status_code == 429
    assert await api.redis.keys("idem:*") == []  # the rate limit runs before the claim


@pytest.mark.parametrize("key", ["", "x" * 256, "a\tb"])
async def test_bad_key_format_is_422(api: Api, key: str) -> None:
    _, headers = await user_headers(api, "owner")
    response = await create(api, headers, key)
    assert response.status_code == 422
    assert response.json()["errors"][0]["loc"] == ["header", "idempotency-key"]


async def test_longest_printable_key_is_accepted(api: Api) -> None:
    _, headers = await user_headers(api, "owner")
    assert (await create(api, headers, "k" * 255)).status_code == 201


async def test_checks_run_before_the_claim(api: Api) -> None:
    owner, headers = await user_headers(api, "owner")
    business_id = await new_business(api, headers)
    _, stranger = await user_headers(api, "owner")
    url = f"/v1/businesses/{business_id}"
    unauthenticated = await api.client.patch(
        url, json={"name": "X"}, headers={"Idempotency-Key": KEY}
    )
    assert unauthenticated.status_code == 401
    forbidden = await api.client.patch(url, json={"name": "X"}, headers=keyed(stranger))
    assert forbidden.status_code == 403
    assert await api.redis.keys("idem:*") == []
    assert owner


async def test_redis_down_fails_closed_with_503(api: Api) -> None:
    _, headers = await user_headers(api, "owner")
    business_id = await new_business(api, headers)
    url = f"/v1/businesses/{business_id}"
    real: Redis = api.app.state.redis
    api.app.state.redis = Redis.from_url("redis://localhost:1/0")  # nothing listens on port 1
    try:
        no_key = await api.client.patch(url, json={"name": "A"}, headers=headers)
        with_key = await api.client.patch(url, json={"name": "B"}, headers=keyed(headers))
    finally:
        await api.app.state.redis.aclose()
        api.app.state.redis = real
    assert no_key.status_code == 200  # no key, no Redis needed
    assert with_key.status_code == 503 and with_key.json()["code"] == "SERVICE_UNAVAILABLE"


async def test_stored_response_keeps_the_configured_ttl(api: Api) -> None:
    owner, headers = await user_headers(api, "owner")
    await create(api, headers)
    (redis_key,) = await api.redis.keys("idem:*")
    ttl_ms = await api.redis.pttl(redis_key)
    assert 86_400_000 - 10_000 < ttl_ms <= 86_400_000
    await api.redis.delete(redis_key)  # the entry expired
    again = await create(api, headers)
    assert again.status_code == 201 and "Idempotent-Replayed" not in again.headers
    assert await business_count(api, owner) == 2


async def test_failing_to_store_returns_the_real_response_and_keeps_the_claim(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, headers = await user_headers(api, "owner")

    async def broken(*args: object, **kwargs: object) -> None:
        raise RedisError("down")

    monkeypatch.setattr(idempotency, "_swap", broken)
    first = await create(api, headers)
    assert first.status_code == 201
    monkeypatch.undo()
    retry = await create(api, headers)
    assert retry.status_code == 409 and retry.json()["in_progress"] is True  # within the claim TTL
    assert await business_count(api, owner) == 1


async def test_staff_password_never_appears_in_replays_or_redis(api: Api) -> None:
    _, headers = await user_headers(api, "owner")
    business_id = await new_business(api, headers)
    url = f"/v1/businesses/{business_id}/staff"
    body = {"name": "Sam", "email": new_email(), "initial_password": PASSWORD}
    first = await api.client.post(url, json=body, headers=keyed(headers, "staff-key-001"))
    replay = await api.client.post(url, json=body, headers=keyed(headers, "staff-key-001"))
    assert first.status_code == replay.status_code == 201
    assert replay.headers["Idempotent-Replayed"] == "true" and replay.json() == first.json()
    assert PASSWORD not in replay.text and "set-cookie" not in replay.headers
    keys = await api.redis.keys("idem:*")
    assert len(keys) == 1
    stored = ((await api.redis.get(keys[0])) or b"").decode()
    assert "staff-key-001" not in keys[0].decode()  # the client's key is hashed
    assert PASSWORD not in keys[0].decode() + stored
    assert "initial_password" not in stored  # only the response is kept, never the request


async def test_endpoints_without_the_key_header_and_auth_endpoints_are_unaffected(api: Api) -> None:
    response = await api.client.post(
        "/v1/auth/register",
        json={"name": "N", "email": new_email(), "password": PASSWORD},
        headers={"Idempotency-Key": KEY},
    )
    assert response.status_code == 201 and "Idempotent-Replayed" not in response.headers
    assert await api.redis.keys("idem:*") == []
