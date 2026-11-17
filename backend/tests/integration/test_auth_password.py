import httpx
import jwt
import pytest
from argon2 import PasswordHasher
from sqlalchemy import select

from app.core import security
from app.core.security import hash_password, password_needs_rehash
from app.models import User
from app.services import auth as auth_service
from tests.integration.api import Api, new_email, new_phone, open_api

pytestmark = pytest.mark.anyio

PASSWORD = "correct horse battery"
CHEAP_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


async def register(api: Api, email: str | None = None, password: str = PASSWORD) -> tuple[str, int]:
    email = email or new_email()
    response = await api.client.post(
        "/v1/auth/register", json={"name": "Owner", "email": email, "password": password}
    )
    return email, response.status_code


async def post_login(api: Api, email: str, password: str) -> httpx.Response:
    return await api.client.post("/v1/auth/login", json={"email": email, "password": password})


async def get_user(api: Api, email: str) -> User:
    async with api.sessions() as session:
        return (await session.execute(select(User).where(User.email == email))).scalar_one()


async def test_register_creates_an_owner_without_tokens_or_hash(api: Api) -> None:
    email = new_email()
    response = await api.client.post(
        "/v1/auth/register", json={"name": "Owner", "email": email, "password": PASSWORD}
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"user"}
    assert body["user"] | {"id": None} == {
        "id": None, "name": "Owner", "email": email, "phone": None, "role": "owner",
    }  # fmt: skip
    assert "password" not in response.text and "refresh_token" not in api.client.cookies
    user = await get_user(api, email)
    assert user.role == "owner" and user.password_hash is not None
    assert user.password_hash.startswith("$argon2id$") and PASSWORD not in user.password_hash


async def test_register_ignores_a_requested_role(api: Api) -> None:
    response = await api.client.post(
        "/v1/auth/register",
        json={"name": "x", "email": new_email(), "password": PASSWORD, "role": "admin"},
    )
    assert response.json()["user"]["role"] == "owner"


async def test_duplicate_email_is_a_validation_error_case_insensitively(api: Api) -> None:
    email, _ = await register(api)
    response = await api.client.post(
        "/v1/auth/register",
        json={"name": "Again", "email": email.upper(), "password": PASSWORD},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert response.json()["errors"][0]["loc"] == ["body", "email"]


@pytest.mark.parametrize("password", ["x" * 9, "x" * 129])
async def test_weak_or_oversize_password_is_rejected_without_echo(api: Api, password: str) -> None:
    email, status = await register(api, password=password)
    assert status == 422
    response = await api.client.post(
        "/v1/auth/register", json={"name": "x", "email": email, "password": password}
    )
    assert password not in response.text


async def test_register_per_ip_limit(api: Api) -> None:
    for _ in range(10):
        await register(api, password="short")
    assert (await register(api, password="short"))[1] == 429


async def test_login_issues_a_session_for_owner_staff_and_admin(api: Api) -> None:
    for role in ("owner", "staff", "admin"):
        email = new_email()
        await api.create_user(role, email=email, password_hash=hash_password(PASSWORD))
        response = await post_login(api, email, PASSWORD)
        assert response.status_code == 200, role
        body = response.json()
        claims = jwt.decode(body["access_token"], api.settings.jwt_secret, algorithms=["HS256"])
        assert claims["role"] == role and body["user"]["email"] == email
        assert "password" not in response.text
        assert api.client.cookies["refresh_token"] not in response.text


async def test_login_is_case_insensitive_on_email(api: Api) -> None:
    email, _ = await register(api)
    assert (await post_login(api, email.upper(), PASSWORD)).status_code == 200


async def test_wrong_password_and_unknown_email_are_indistinguishable(api: Api) -> None:
    email, _ = await register(api)
    wrong = await post_login(api, email, "not the password")
    unknown = await post_login(api, new_email(), "not the password")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() and wrong.json()["code"] == "UNAUTHENTICATED"
    assert "set-cookie" not in wrong.headers and "set-cookie" not in unknown.headers


async def test_unknown_email_still_pays_for_one_argon2_verification(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = security.verify_password

    def spy(password_hash: str, password: str) -> bool:
        calls.append(password_hash)
        return real(password_hash, password)

    monkeypatch.setattr("app.services.auth.verify_password", spy)
    email, _ = await register(api)
    await post_login(api, email, "wrong password")
    await post_login(api, new_email(), "wrong password")
    assert len(calls) == 2 and calls[1] == auth_service.DUMMY_PASSWORD_HASH != calls[0]


async def test_a_customer_cannot_password_login(api: Api) -> None:
    email = new_email()
    await api.create_user(
        "customer", email=email, phone=new_phone(), password_hash=hash_password(PASSWORD)
    )
    response = await post_login(api, email, PASSWORD)
    unknown = await post_login(api, new_email(), PASSWORD)
    assert response.status_code == 401 and response.json() == unknown.json()


async def test_a_user_without_a_password_cannot_login(api: Api) -> None:
    email = new_email()
    await api.create_user("owner", email=email)
    assert (await post_login(api, email, PASSWORD)).status_code == 401


async def test_login_rehashes_an_outdated_hash_only_on_success(api: Api) -> None:
    email = new_email()
    old_hash = CHEAP_HASHER.hash(PASSWORD)
    await api.create_user("owner", email=email, password_hash=old_hash)
    assert password_needs_rehash(old_hash)
    await post_login(api, email, "wrong password")
    assert (await get_user(api, email)).password_hash == old_hash
    assert (await post_login(api, email, PASSWORD)).status_code == 200
    new_hash = (await get_user(api, email)).password_hash
    assert new_hash is not None and new_hash != old_hash and not password_needs_rehash(new_hash)
    assert (await post_login(api, email, PASSWORD)).status_code == 200


async def test_login_per_email_limit_is_five_per_window(api: Api) -> None:
    email, _ = await register(api)
    for _ in range(5):
        assert (await post_login(api, email, "wrong password")).status_code == 401
    blocked = await post_login(api, email, PASSWORD)  # even the right password
    assert blocked.status_code == 429 and blocked.json()["code"] == "RATE_LIMITED"
    assert int(blocked.headers["retry-after"]) > 0
    other, _ = await register(api)
    assert (await post_login(api, other, PASSWORD)).status_code == 200
    api.clock.now += 901
    assert (await post_login(api, email, PASSWORD)).status_code == 200


async def test_login_per_ip_limit_is_twenty_per_window(api: Api) -> None:
    for _ in range(20):
        assert (await post_login(api, new_email(), PASSWORD)).status_code == 401
    assert (await post_login(api, new_email(), PASSWORD)).status_code == 429


async def test_login_rate_limit_key_has_no_raw_email(api: Api) -> None:
    email = new_email()
    await post_login(api, email, PASSWORD)
    keys = [key async for key in api.redis.scan_iter("rl:login_email:*")]
    assert keys and all(email.encode() not in key for key in keys)


async def test_a_lowered_ip_limit_is_the_one_enforced(migrated_url: str, redis_url: str) -> None:
    """The setting is not decoration: the 429 arrives at the configured limit, not at 20."""
    async with open_api(migrated_url, redis_url, {"login_ip_limit": 3}) as api:
        for _ in range(3):
            assert (await post_login(api, new_email(), PASSWORD)).status_code == 401
        blocked = await post_login(api, new_email(), PASSWORD)
        assert blocked.status_code == 429 and blocked.json()["code"] == "RATE_LIMITED"


async def test_a_lowered_email_limit_is_the_one_enforced(migrated_url: str, redis_url: str) -> None:
    async with open_api(migrated_url, redis_url, {"login_email_limit": 2}) as api:
        email, _ = await register(api)
        for _ in range(2):
            assert (await post_login(api, email, "wrong password")).status_code == 401
        blocked = await post_login(api, email, PASSWORD)  # even the right password
        assert blocked.status_code == 429 and blocked.json()["code"] == "RATE_LIMITED"


async def test_a_raised_ip_limit_is_the_one_enforced(migrated_url: str, redis_url: str) -> None:
    """What the e2e stack does: 25 logins from one IP pass where the default would stop at 20."""
    async with open_api(migrated_url, redis_url, {"login_ip_limit": 200}) as api:
        for _ in range(25):
            assert (await post_login(api, new_email(), PASSWORD)).status_code == 401
