import uuid

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.exc import IntegrityError

from app.core.security import create_access_token
from app.models import Business, BusinessMember, Resource, Service, User
from app.schemas.business import BusinessCreate
from app.services import business as business_service
from tests.integration.api import Api, new_email

pytestmark = pytest.mark.anyio

OPEN_HOURS = {"mon": [{"open": "09:00", "close": "17:00"}]}
SERVICE = {"name": "Checkup", "duration_min": 15, "price": "250.00"}


def business_body(**overrides: object) -> dict[str, object]:
    return {
        "name": "Clinic",
        "category": "clinic",
        "timezone": "Asia/Kolkata",
        "open_hours": OPEN_HOURS,
    } | overrides


async def user_headers(api: Api, role: str) -> tuple[User, dict[str, str]]:
    user = await api.create_user(role, email=new_email())
    token = create_access_token(api.settings, user.id, role)
    return user, {"Authorization": f"Bearer {token}"}


async def new_business(api: Api, headers: dict[str, str], **overrides: object) -> uuid.UUID:
    response = await api.client.post(
        "/v1/businesses", json=business_body(**overrides), headers=headers
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def new_service(
    api: Api, business_id: uuid.UUID, headers: dict[str, str], **body: object
) -> str:
    response = await api.client.post(
        f"/v1/businesses/{business_id}/services", json=SERVICE | body, headers=headers
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def test_create_business_seeds_settings_resource_and_owner_membership(api: Api) -> None:
    owner, headers = await user_headers(api, "owner")
    business_id = await new_business(api, headers)
    async with api.sessions() as session:
        business = await session.get(Business, business_id)
        assert business is not None
        assert business.owner_id == owner.id
        assert business.settings == business_service.default_settings()
        assert business.settings["walk_in_ratio"] == {"walk_ins": 1, "appointments": 2}
        assert business.settings["max_walk_ins"] == 50
        resources = (
            await session.scalars(select(Resource.name).where(Resource.business_id == business_id))
        ).all()
        assert resources == ["Main"]
        member = await session.get(BusinessMember, (business_id, owner.id))
        assert member is not None and member.role == "owner"


async def test_create_business_auth_order(api: Api) -> None:
    assert (await api.client.post("/v1/businesses", json=business_body())).status_code == 401
    for role in ("customer", "staff", "admin"):
        _, headers = await user_headers(api, role)
        response = await api.client.post("/v1/businesses", json=business_body(), headers=headers)
        assert response.status_code == 403, role
    _, headers = await user_headers(api, "owner")
    response = await api.client.post(
        "/v1/businesses", json=business_body(timezone="Mars/Base"), headers=headers
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_role_comes_from_the_database_not_the_claim(api: Api) -> None:
    customer = await api.create_user("customer", email=new_email())
    forged = create_access_token(api.settings, customer.id, "owner")
    response = await api.client.post(
        "/v1/businesses", json=business_body(), headers={"Authorization": f"Bearer {forged}"}
    )
    assert response.status_code == 403


async def test_create_business_rate_limit_precedes_validation(api: Api) -> None:
    _, headers = await user_headers(api, "owner")
    for _ in range(10):  # invalid bodies still count: the limit runs before validation
        response = await api.client.post("/v1/businesses", json={}, headers=headers)
        assert response.status_code == 422
    response = await api.client.post("/v1/businesses", json=business_body(), headers=headers)
    assert response.status_code == 429
    assert "Retry-After" in response.headers
    _, other = await user_headers(api, "owner")  # per user, not global
    await new_business(api, other)


async def test_create_business_is_atomic(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = await api.create_user("owner", email=new_email())
    name = f"atomic-{uuid.uuid4().hex}"
    statements: list[str] = []
    engine = api.app.state.engine.sync_engine

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    # A NULL resource name violates NOT NULL after the business row is already inserted.
    monkeypatch.setattr(business_service, "DEFAULT_RESOURCE_NAME", None)
    event.listen(engine, "before_cursor_execute", record)
    try:
        async with api.sessions() as session:
            with pytest.raises(IntegrityError, match="null value"):
                await business_service.create_business(
                    session, owner.id, BusinessCreate.model_validate(business_body(name=name))
                )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    inserts = [s.split()[2] for s in statements if s.startswith("INSERT")]
    assert inserts[0] == "businesses" and "resources" in inserts  # failed after the business row
    async with api.sessions() as session:
        assert await session.scalar(select(Business).where(Business.name == name)) is None


async def test_list_businesses_paginates_by_name_then_id(api: Api) -> None:
    _, headers = await user_headers(api, "owner")
    category = uuid.uuid4().hex[:12]
    ids = {
        await new_business(api, headers, name=name, category=category) for name in ("B", "A", "B")
    }
    seen: list[tuple[str, str]] = []
    cursor = None
    while True:
        params = {"category": category, "limit": "2"} | ({"cursor": cursor} if cursor else {})
        page = (await api.client.get("/v1/businesses", params=params)).json()
        seen += [(item["name"], item["id"]) for item in page["items"]]
        assert "settings" not in page["items"][0]
        if not (cursor := page["next_cursor"]):
            break
    assert seen == sorted(seen)
    assert {uuid.UUID(business_id) for _, business_id in seen} == ids


async def test_list_businesses_search_escapes_wildcards(api: Api) -> None:
    _, headers = await user_headers(api, "owner")
    category = uuid.uuid4().hex[:12]
    await new_business(api, headers, name="100% Cuts", category=category)
    await new_business(api, headers, name="100 Cuts", category=category)
    page = (await api.client.get("/v1/businesses", params={"category": category, "q": "0%"})).json()
    assert [item["name"] for item in page["items"]] == ["100% Cuts"]
    page = (await api.client.get("/v1/businesses", params={"category": category, "q": "_"})).json()
    assert page["items"] == []


async def test_bad_cursor_is_422(api: Api) -> None:
    response = await api.client.get("/v1/businesses", params={"cursor": "garbage!"})
    assert response.status_code == 422
    assert response.json()["errors"] == [{"loc": ["query", "cursor"], "msg": "Invalid cursor"}]


async def test_get_business(api: Api) -> None:
    _, headers = await user_headers(api, "owner")
    business_id = await new_business(api, headers)
    response = await api.client.get(f"/v1/businesses/{business_id}")
    assert response.status_code == 200
    assert response.json()["timezone"] == "Asia/Kolkata"
    response = await api.client.get(f"/v1/businesses/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


async def test_service_crud_and_public_list(api: Api) -> None:
    _, headers = await user_headers(api, "owner")
    business_id = await new_business(api, headers)
    url = f"/v1/businesses/{business_id}/services"
    service_id = await new_service(api, business_id, headers)
    hidden_id = await new_service(api, business_id, headers, name="Retired", is_active=False)
    response = await api.client.patch(f"{url}/{service_id}", json={"price": "300"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["price"] == "300.00"
    public = (await api.client.get(url)).json()
    assert [service["id"] for service in public] == [service_id]
    owner_view = (
        await api.client.get(url, params={"include_inactive": "true"}, headers=headers)
    ).json()
    assert {service["id"] for service in owner_view} == {service_id, hidden_id}
    response = await api.client.patch(f"{url}/{service_id}", json={"name": None}, headers=headers)
    assert response.status_code == 422
    assert (await api.client.get(f"/v1/businesses/{uuid.uuid4()}/services")).status_code == 404


async def test_include_inactive_is_owner_only(api: Api) -> None:
    _, owner = await user_headers(api, "owner")
    business_id = await new_business(api, owner)
    url = f"/v1/businesses/{business_id}/services"
    params = {"include_inactive": "true"}
    assert (await api.client.get(url, params=params)).status_code == 401
    unknown = f"/v1/businesses/{uuid.uuid4()}/services"
    assert (await api.client.get(unknown, params=params, headers=owner)).status_code == 404
    _, stranger = await user_headers(api, "owner")
    assert (await api.client.get(url, params=params, headers=stranger)).status_code == 403


async def test_service_writes_are_owner_member_only(api: Api) -> None:
    _, owner = await user_headers(api, "owner")
    business_id = await new_business(api, owner)
    service_id = await new_service(api, business_id, owner)
    url = f"/v1/businesses/{business_id}/services"
    staff, staff_headers = await user_headers(api, "staff")
    async with api.sessions() as session:
        session.add(BusinessMember(business_id=business_id, user_id=staff.id, role="staff"))
        await session.commit()
    _, other_owner = await user_headers(api, "owner")
    _, admin = await user_headers(api, "admin")
    assert (await api.client.post(url, json=SERVICE)).status_code == 401
    for headers in (other_owner, staff_headers, admin):  # admin is not a superuser
        assert (await api.client.post(url, json=SERVICE, headers=headers)).status_code == 403
        response = await api.client.patch(
            f"{url}/{service_id}", json={"price": "1"}, headers=headers
        )
        assert response.status_code == 403
        assert response.json()["code"] == "FORBIDDEN"
    unknown = f"/v1/businesses/{uuid.uuid4()}/services"
    assert (await api.client.post(unknown, json=SERVICE, headers=owner)).status_code == 404


async def test_membership_is_checked_on_every_call(api: Api) -> None:
    owner, headers = await user_headers(api, "owner")
    business_id = await new_business(api, headers)
    await new_service(api, business_id, headers)
    async with api.sessions() as session:
        await session.execute(
            delete(BusinessMember).where(
                BusinessMember.business_id == business_id, BusinessMember.user_id == owner.id
            )
        )
        await session.commit()
    url = f"/v1/businesses/{business_id}/services"
    assert (await api.client.post(url, json=SERVICE, headers=headers)).status_code == 403


async def test_cross_tenant_service_is_404(api: Api) -> None:
    _, owner_a = await user_headers(api, "owner")
    _, owner_b = await user_headers(api, "owner")
    business_a = await new_business(api, owner_a)
    business_b = await new_business(api, owner_b)
    service_b = await new_service(api, business_b, owner_b)
    response = await api.client.patch(
        f"/v1/businesses/{business_a}/services/{service_b}", json={"price": "1"}, headers=owner_a
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
    async with api.sessions() as session:
        service = await session.get(Service, uuid.UUID(service_b))
        assert service is not None and str(service.price) == "250.00"


async def test_service_write_rate_limit_after_auth_before_validation(api: Api) -> None:
    _, owner = await user_headers(api, "owner")
    business_id = await new_business(api, owner)
    url = f"/v1/businesses/{business_id}/services"
    _, stranger = await user_headers(api, "owner")
    for _ in range(61):  # forbidden calls never reach the limiter
        assert (await api.client.post(url, json=SERVICE, headers=stranger)).status_code == 403
    for _ in range(30):
        assert (await api.client.post(url, json={}, headers=owner)).status_code == 422
    for _ in range(30):
        response = await api.client.patch(f"{url}/{uuid.uuid4()}", json={}, headers=owner)
        assert response.status_code == 404
    response = await api.client.post(url, json=SERVICE, headers=owner)
    assert response.status_code == 429
    assert "Retry-After" in response.headers


async def test_public_search_is_rate_limited_but_id_reads_are_not(api: Api) -> None:
    _, headers = await user_headers(api, "owner")
    business_id = await new_business(api, headers)
    for _ in range(61):  # primary-key reads stay unlimited
        assert (await api.client.get(f"/v1/businesses/{business_id}")).status_code == 200
        assert (await api.client.get(f"/v1/businesses/{business_id}/services")).status_code == 200
    for _ in range(60):
        assert (await api.client.get("/v1/businesses", params={"limit": "1"})).status_code == 200
    blocked = await api.client.get("/v1/businesses", params={"limit": "1"})
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "RATE_LIMITED" and "Retry-After" in blocked.headers
    api.clock.now += 61  # past the 60 s window
    assert (await api.client.get("/v1/businesses", params={"limit": "1"})).status_code == 200
