import asyncio
import uuid

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from app.models import BusinessMember, User
from app.schemas.business import StaffCreate
from app.services import business as business_service
from tests.integration.api import Api, new_email
from tests.integration.test_businesses import business_body, new_business, user_headers

pytestmark = pytest.mark.anyio

PASSWORD = "initial-pass-123"


async def new_staff(
    api: Api, owner: dict[str, str], business_id: uuid.UUID, email: str | None = None
) -> dict[str, str]:
    response = await api.client.post(
        f"/v1/businesses/{business_id}/staff",
        json={"name": "Sam", "email": email or new_email(), "initial_password": PASSWORD},
        headers=owner,
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def login(api: Api, email: str, password: str = PASSWORD) -> dict[str, str]:
    response = await api.client.post("/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def settings_of(api: Api, owner: dict[str, str], business_id: uuid.UUID) -> dict[str, object]:
    response = await api.client.get(f"/v1/businesses/{business_id}/settings", headers=owner)
    assert response.status_code == 200
    return response.json()["settings"]  # type: ignore[no-any-return]


async def test_patch_each_field_alone_and_combined(api: Api) -> None:
    _, owner = await user_headers(api, "owner")
    business_id = await new_business(api, owner)
    url = f"/v1/businesses/{business_id}"
    hours = {"tue": [{"open": "10:00", "close": "12:00"}]}
    for body in (
        {"name": "New"},
        {"address": "1 Main St"},
        {"timezone": "Europe/London"},
        {"open_hours": hours},
    ):
        response = await api.client.patch(url, json=body, headers=owner)
        assert response.status_code == 200, (body, response.text)
        key = next(iter(body))
        assert response.json()[key] == body[key]
    response = await api.client.patch(url, json={"settings": {"max_walk_ins": 7}}, headers=owner)
    assert response.json()["settings"]["max_walk_ins"] == 7
    response = await api.client.patch(url, json={"address": None, "name": "Both"}, headers=owner)
    assert response.json()["address"] is None and response.json()["name"] == "Both"
    assert "owner_id" not in response.json()
    assert response.json()["settings"]["max_walk_ins"] == 7


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"unknown": 1},
        {"category": "x"},
        {"name": None},
        {"settings": {}},
        {"settings": None},
        {"settings": {"nope": 1}},
        {"settings": {"grace_period_min": 61}},
        {"settings": {"grace_period_min": True}},
        {"settings": {"grace_period_min": 5.0}},
        {"settings": {"walk_in_ratio": {"walk_ins": 1}}},
    ],
)
async def test_patch_invalid_bodies_are_422(api: Api, body: dict[str, object]) -> None:
    _, owner = await user_headers(api, "owner")
    business_id = await new_business(api, owner)
    response = await api.client.patch(f"/v1/businesses/{business_id}", json=body, headers=owner)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_settings_merge_keeps_other_keys_and_replaces_ratio_whole(api: Api) -> None:
    _, owner = await user_headers(api, "owner")
    business_id = await new_business(api, owner)
    url = f"/v1/businesses/{business_id}"
    await api.client.patch(
        url, json={"settings": {"walk_in_ratio": {"walk_ins": 3, "appointments": 4}}}, headers=owner
    )
    await api.client.patch(url, json={"settings": {"grace_period_min": 9}}, headers=owner)
    assert await settings_of(api, owner, business_id) == business_service.default_settings() | {
        "walk_in_ratio": {"walk_ins": 3, "appointments": 4},
        "grace_period_min": 9,
    }


async def test_concurrent_patches_of_different_keys_both_survive(api: Api) -> None:
    _, owner = await user_headers(api, "owner")
    business_id = await new_business(api, owner)
    url = f"/v1/businesses/{business_id}"
    keys = [
        "grace_period_min",
        "reschedule_cutoff_min",
        "max_walk_ins",
        "running_late_threshold_min",
    ]
    responses = await asyncio.gather(
        *(
            api.client.patch(url, json={"settings": {key: 1 + i}}, headers=owner)
            for i, key in enumerate(keys)
        )
    )
    assert [r.status_code for r in responses] == [200] * len(keys)
    final = await settings_of(api, owner, business_id)
    assert {key: final[key] for key in keys} == {key: 1 + i for i, key in enumerate(keys)}


async def test_patch_and_reads_authorization(api: Api) -> None:
    _, owner = await user_headers(api, "owner")
    business_id = await new_business(api, owner)
    staff = await new_staff(api, owner, business_id)
    staff_headers = await login(api, staff["email"])
    _, other_owner = await user_headers(api, "owner")
    _, customer = await user_headers(api, "customer")
    _, admin = await user_headers(api, "admin")
    calls = [
        ("PATCH", f"/v1/businesses/{business_id}", {"name": "X"}),
        ("GET", f"/v1/businesses/{business_id}/settings", None),
        ("GET", f"/v1/businesses/{business_id}/staff", None),
    ]
    for method, url, body in calls:
        assert (await api.client.request(method, url, json=body)).status_code == 401
        for headers in (other_owner, staff_headers, customer, admin):
            response = await api.client.request(method, url, json=body, headers=headers)
            assert response.status_code == 403, (method, url)
        missing = url.replace(str(business_id), str(uuid.uuid4()))
        assert (
            await api.client.request(method, missing, json=body, headers=owner)
        ).status_code == 404


async def test_staff_lifecycle_and_removal_takes_effect_immediately(api: Api) -> None:
    _, owner = await user_headers(api, "owner")
    business_id = await new_business(api, owner, name="Mine A")
    other_id = await new_business(api, owner, name="Mine B")
    staff = await new_staff(api, owner, business_id)
    assert set(staff) == {"id", "name", "email", "role"} and staff["role"] == "staff"
    headers = await login(api, staff["email"])
    mine = await api.client.get("/v1/businesses/mine", headers=headers)
    assert [(b["id"], b["role"]) for b in mine.json()] == [(str(business_id), "staff")]
    for url in (f"/v1/businesses/{business_id}/staff", f"/v1/businesses/{other_id}/staff"):
        assert (await api.client.get(url, headers=headers)).status_code == 403
    listing = await api.client.get(f"/v1/businesses/{business_id}/staff", headers=owner)
    assert listing.json() == [{"id": staff["id"], "name": "Sam", "email": staff["email"]}]
    assert "password" not in listing.text and "phone" not in listing.text

    url = f"/v1/businesses/{business_id}/staff/{staff['id']}"
    assert (await api.client.delete(url, headers=owner)).status_code == 204
    assert (await api.client.delete(url, headers=owner)).status_code == 404
    # the access token is still valid, but membership is read on every call
    assert (await api.client.get("/v1/businesses/mine", headers=headers)).json() == []
    assert (
        await api.client.get(f"/v1/businesses/{business_id}/staff", headers=headers)
    ).status_code == 403
    async with api.sessions() as session:
        assert await session.scalar(select(User.id).where(User.id == uuid.UUID(staff["id"])))
    await login(api, staff["email"])  # can still log in; just has no business


async def test_mine_for_owner_and_customer(api: Api) -> None:
    _, owner = await user_headers(api, "owner")
    ids = {str(await new_business(api, owner, name=n)) for n in ("A1", "A2")}
    response = await api.client.get("/v1/businesses/mine", headers=owner)
    assert {b["id"] for b in response.json()} == ids
    assert {b["role"] for b in response.json()} == {"owner"}
    assert "settings" not in response.json()[0]
    _, customer = await user_headers(api, "customer")
    assert (await api.client.get("/v1/businesses/mine", headers=customer)).status_code == 403
    assert (await api.client.get("/v1/businesses/mine")).status_code == 401


async def test_delete_only_removes_staff_of_this_business(api: Api) -> None:
    owner_user, owner = await user_headers(api, "owner")
    business_id = await new_business(api, owner)
    other_id = await new_business(api, owner)
    other_staff = await new_staff(api, owner, other_id)
    base = f"/v1/businesses/{business_id}/staff"
    for target in (owner_user.id, uuid.UUID(other_staff["id"]), uuid.uuid4()):
        assert (await api.client.delete(f"{base}/{target}", headers=owner)).status_code == 404
    async with api.sessions() as session:
        assert await session.get(BusinessMember, (business_id, owner_user.id)) is not None
        assert (
            await session.get(BusinessMember, (other_id, uuid.UUID(other_staff["id"]))) is not None
        )
    _, stranger = await user_headers(api, "owner")
    assert (
        await api.client.delete(f"{base}/{other_staff['id']}", headers=stranger)
    ).status_code == 403


async def test_staff_email_is_never_reused_or_attached(api: Api) -> None:
    owner_user, owner = await user_headers(api, "owner")
    business_id = await new_business(api, owner)
    other_id = await new_business(api, owner)
    first = await new_staff(api, owner, other_id)
    taken = [owner_user.email, first["email"]]
    register = await api.client.post(
        "/v1/auth/register", json={"name": "N", "email": owner_user.email, "password": PASSWORD}
    )
    assert register.status_code == 422
    for email in taken:
        response = await api.client.post(
            f"/v1/businesses/{business_id}/staff",
            json={"name": "X", "email": email, "initial_password": PASSWORD},
            headers=owner,
        )
        assert response.status_code == 422
        assert response.json()["errors"] == register.json()["errors"]
    async with api.sessions() as session:
        assert await session.get(BusinessMember, (business_id, uuid.UUID(first["id"]))) is None


async def test_parallel_creates_with_one_email_give_one_success(api: Api) -> None:
    _, owner = await user_headers(api, "owner")
    business_id = await new_business(api, owner)
    email = new_email()
    responses = await asyncio.gather(
        *(
            api.client.post(
                f"/v1/businesses/{business_id}/staff",
                json={"name": "X", "email": email, "initial_password": PASSWORD},
                headers=owner,
            )
            for _ in range(2)
        )
    )
    assert sorted(r.status_code for r in responses) == [201, 422]


async def test_staff_create_is_atomic(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    _, owner_headers = await user_headers(api, "owner")
    business_id = await new_business(api, owner_headers)
    email = new_email()
    statements: list[str] = []
    engine = api.app.state.engine.sync_engine

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    # The role CHECK on business_members fails after the user row is already inserted.
    original = BusinessMember.__init__

    def bad_role(self: BusinessMember, **kwargs: object) -> None:
        original(self, **{**kwargs, "role": "bogus"})

    monkeypatch.setattr(BusinessMember, "__init__", bad_role)
    event.listen(engine, "before_cursor_execute", record)
    try:
        async with api.sessions() as session:
            with pytest.raises(IntegrityError):
                await business_service.create_staff(
                    session,
                    business_id,
                    StaffCreate(name="X", email=email, initial_password=PASSWORD),
                )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert any(s.startswith("INSERT INTO users") for s in statements)
    async with api.sessions() as session:
        assert await session.scalar(select(User.id).where(User.email == email)) is None


async def test_staff_rate_limits(api: Api) -> None:
    _, owner = await user_headers(api, "owner")
    business_id = await new_business(api, owner)
    url = f"/v1/businesses/{business_id}/staff"
    for _ in range(20):
        assert (await api.client.post(url, json={}, headers=owner)).status_code == 422
    assert (await api.client.post(url, json={}, headers=owner)).status_code == 429
    for _ in range(60):
        assert (await api.client.delete(f"{url}/{uuid.uuid4()}", headers=owner)).status_code == 404
    response = await api.client.delete(f"{url}/{uuid.uuid4()}", headers=owner)
    assert response.status_code == 429 and "Retry-After" in response.headers


@pytest.mark.parametrize(
    ("method", "path", "invalid", "valid"),
    [
        ("PATCH", "", {}, {"name": "X"}),
        (
            "POST",
            "/staff",
            {"name": "X", "email": "not-an-email", "initial_password": PASSWORD},
            {"name": "X", "email": "someone@example.test", "initial_password": PASSWORD},
        ),
    ],
)
async def test_authorization_precedes_validation(
    api: Api, method: str, path: str, invalid: dict[str, str], valid: dict[str, str]
) -> None:
    _, owner = await user_headers(api, "owner")
    business_id = await new_business(api, owner)
    _, stranger = await user_headers(api, "owner")
    url = f"/v1/businesses/{business_id}{path}"
    bad_stranger = await api.client.request(method, url, json=invalid, headers=stranger)
    good_stranger = await api.client.request(method, url, json=valid, headers=stranger)
    assert bad_stranger.status_code == good_stranger.status_code == 403
    assert bad_stranger.json() == good_stranger.json()
    unknown = f"/v1/businesses/{uuid.uuid4()}{path}"
    assert (
        await api.client.request(method, unknown, json=invalid, headers=owner)
    ).status_code == 404
    assert (await api.client.request(method, url, json=invalid, headers=owner)).status_code == 422


async def test_empty_address_is_422_and_null_clears(api: Api) -> None:
    _, owner = await user_headers(api, "owner")
    response = await api.client.post(
        "/v1/businesses", json=business_body(address=""), headers=owner
    )
    assert response.status_code == 422
    business_id = await new_business(api, owner, address="1 Main St")
    url = f"/v1/businesses/{business_id}"
    assert (await api.client.patch(url, json={"address": ""}, headers=owner)).status_code == 422
    cleared = await api.client.patch(url, json={"address": None}, headers=owner)
    assert cleared.status_code == 200 and cleared.json()["address"] is None
