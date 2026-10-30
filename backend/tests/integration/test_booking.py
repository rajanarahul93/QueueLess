import asyncio
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError

from app.core.security import create_access_token
from app.models import Appointment, AuditLog, Resource, User
from tests.integration.api import Api, new_email, open_api
from tests.integration.test_availability import (
    CLOCK,
    Headers,
    hours,
    resource_id,
    setup_business,
    utc,
)
from tests.integration.test_business_admin import PASSWORD, login, new_staff
from tests.integration.test_businesses import new_service

pytestmark = pytest.mark.anyio

SERVICE = {"duration_min": 30, "buffer_min": 10}
ALL_DAY = hours(("09:00", "17:00"))
URL = "/v1/appointments"


def local(hour: int, minute: int = 0, day: int = 2) -> str:
    return f"2026-11-{day:02d}T{hour:02d}:{minute:02d}:00+05:30"


class World:
    def __init__(self, api: Api, owner: Headers, business_id: uuid.UUID, service_id: str) -> None:
        self.api, self.owner, self.business_id, self.service_id = (
            api,
            owner,
            business_id,
            service_id,
        )
        self.resource: uuid.UUID

    def body(self, start: str, **overrides: object) -> dict[str, object]:
        return {
            "service_id": self.service_id,
            "resource_id": str(self.resource),
            "start": start,
        } | overrides

    async def book(self, headers: Headers, start: str, **kwargs: Any) -> Any:
        return await self.api.client.post(
            URL, json=self.body(start), headers=headers | kwargs.pop("extra", {})
        )

    async def insert(
        self,
        customer_id: uuid.UUID,
        start: datetime,
        end: datetime,
        status: str = "booked",
        resource: uuid.UUID | None = None,
    ) -> uuid.UUID:
        async with self.api.sessions() as session:
            new_id: uuid.UUID | None = await session.scalar(
                insert(Appointment)
                .values(
                    business_id=self.business_id,
                    service_id=uuid.UUID(self.service_id),
                    resource_id=resource or self.resource,
                    customer_id=customer_id,
                    during=Range(start, end, bounds="[)"),
                    status=status,
                )
                .returning(Appointment.id)
            )
            await session.commit()
            assert new_id is not None
            return new_id

    async def staff(self) -> Headers:
        return await staff_headers(self.api, self.owner, self.business_id)


async def staff_headers(api: Api, owner: Headers, business_id: uuid.UUID) -> Headers:
    created = await new_staff(api, owner, business_id)
    saved = api.clock.now
    api.clock.now = time.time()  # login stamps iat with the app clock; JWT checks use real time
    try:
        return await login(api, created["email"], PASSWORD)
    finally:
        api.clock.now = saved


async def world(api: Api, **kwargs: Any) -> World:
    owner, business_id, service_id = await setup_business(
        api,
        open_hours=kwargs.pop("open_hours", ALL_DAY),
        service=SERVICE | kwargs.pop("service", {}),
        **kwargs,
    )
    built = World(api, owner, business_id, service_id)
    built.resource = await resource_id(api, business_id)
    return built


async def customer(api: Api) -> tuple[User, Headers]:
    user = await api.create_user("customer", phone=f"+91{uuid.uuid4().int % 10**10:010d}")
    token = create_access_token(api.settings, user.id, "customer")
    return user, {"Authorization": f"Bearer {token}"}


async def admin(api: Api) -> Headers:
    user = await api.create_user("admin", email=new_email())
    return {"Authorization": f"Bearer {create_access_token(api.settings, user.id, 'admin')}"}


def code(response: Any) -> str:
    return str(response.json()["code"])


async def test_happy_path_returns_display_fields_and_stores_the_buffer(api: Api) -> None:
    w = await world(api)
    user, headers = await customer(api)
    response = await w.book(headers, local(9))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["start"] == local(9)
    assert body["end"] == local(9, 30)  # start + duration, not the stored end
    assert body["status"] == "booked"
    assert body["service_name"] == "Checkup"
    assert body["business_id"] == str(w.business_id)
    assert body["resource_id"] == str(w.resource)
    assert body["created_at"].endswith("+05:30")
    assert "customer" not in body
    async with api.sessions() as session:
        row = await session.get(Appointment, uuid.UUID(body["id"]))
        assert row is not None
        assert row.customer_id == user.id
        assert (row.during.lower, row.during.upper) == (utc(9), utc(9, 40))  # duration + buffer
        assert row.status == "booked"


@pytest.mark.parametrize(
    ("start", "message"),
    [
        ("2026-10-31T09:00:00+05:30", "past"),  # before the injected clock
        ("2027-01-15T09:00:00+05:30", "ahead"),
        (local(9, 7), "available"),  # off the 15-minute grid
        (local(9, 0).replace(":00+", ":30+"), "available"),  # seconds are off the grid
        (local(8, 45), "available"),  # before opening
        (local(16, 45), "available"),  # 30 minutes do not fit before closing
        (local(9, day=3), "available"),  # Tuesday is closed
        ("2026-11-02T09:00:00", "timezone"),  # naive timestamp
    ],
)
async def test_start_validation_failures_are_422_on_body_start(
    api: Api, start: str, message: str
) -> None:
    w = await world(api)
    _, headers = await customer(api)
    response = await w.book(headers, start)
    assert response.status_code == 422, response.text
    assert code(response) == "VALIDATION_ERROR"
    errors = response.json()["errors"]
    assert errors[0]["loc"] == ["body", "start"]
    assert message in errors[0]["msg"].lower()


async def test_service_that_does_not_fit_in_an_interval_is_422(api: Api) -> None:
    w = await world(api, open_hours=hours(("09:00", "09:20")))
    _, headers = await customer(api)
    response = await w.book(headers, local(9))
    assert response.status_code == 422 and response.json()["errors"][0]["loc"] == ["body", "start"]


async def test_unknown_foreign_and_inactive_ids_are_404(api: Api) -> None:
    w = await world(api)
    _, headers = await customer(api)
    other = await world(api)
    inactive = await new_service(api, w.business_id, w.owner, name="Old", duration_min=30)
    await api.client.patch(
        f"/v1/businesses/{w.business_id}/services/{inactive}",
        json={"is_active": False},
        headers=w.owner,
    )
    async with api.sessions() as session:
        gone = await new_resource(session, w.business_id, active=False)
    cases = {
        "unknown service": w.body(local(9), service_id=str(uuid.uuid4())),
        "foreign service": w.body(local(9), service_id=other.service_id),
        "inactive service": w.body(local(9), service_id=inactive),
        "unknown resource": w.body(local(9), resource_id=str(uuid.uuid4())),
        "foreign resource": w.body(local(9), resource_id=str(other.resource)),
        "inactive resource": w.body(local(9), resource_id=str(gone)),
    }
    for name, body in cases.items():
        response = await api.client.post(URL, json=body, headers=headers)
        assert response.status_code == 404, (name, response.text)
    assert (await w.book(headers, local(9))).status_code == 201  # nothing above consumed the slot


async def new_resource(session: Any, business_id: uuid.UUID, *, active: bool) -> uuid.UUID:
    created: uuid.UUID | None = await session.scalar(
        insert(Resource)
        .values(business_id=business_id, name="Extra", is_active=active)
        .returning(Resource.id)
    )
    await session.commit()
    assert created is not None
    return created


@pytest.mark.parametrize("body", [{"service_id": "x"}, {"extra": 1}, {"start": None}])
async def test_malformed_body_is_422(api: Api, body: dict[str, object]) -> None:
    w = await world(api)
    _, headers = await customer(api)
    response = await api.client.post(URL, json=w.body(local(9)) | body, headers=headers)
    assert response.status_code == 422 and code(response) == "VALIDATION_ERROR"


async def test_each_role_and_unauthenticated(api: Api) -> None:
    w = await world(api)
    staff = await w.staff()
    for headers in (w.owner, staff, await admin(api)):
        assert (await w.book(headers, local(9))).status_code == 403
    garbage = await api.client.post(URL, json={"junk": 1})
    assert garbage.status_code == 401  # authentication comes before any validation
    assert (await api.client.post(URL, json={"junk": 1}, headers=w.owner)).status_code == 403


async def test_taken_slot_is_409_with_the_next_three_slots_on_that_date(api: Api) -> None:
    w = await world(api)
    _, first = await customer(api)
    _, second = await customer(api)
    assert (await w.book(first, local(9))).status_code == 201
    response = await w.book(second, local(9))
    assert response.status_code == 409 and code(response) == "SLOT_TAKEN"
    slots = response.json()["next_slots"]
    # The stored range [09:00, 09:40) blocks starts before 09:45.
    assert [s["start"] for s in slots] == [local(9, 45), local(10), local(10, 15)]
    assert slots[0] == {"start": local(9, 45), "end": local(10, 15), "resource_id": str(w.resource)}
    assert not any(s["start"] == local(9) for s in slots)


async def test_taken_slot_with_nothing_later_has_an_empty_list(api: Api) -> None:
    w = await world(api, open_hours=hours(("09:00", "09:30")))
    _, first = await customer(api)
    _, second = await customer(api)
    assert (await w.book(first, local(9))).status_code == 201
    response = await w.book(second, local(9))
    assert response.status_code == 409 and response.json()["next_slots"] == []


async def test_buffer_blocks_a_start_inside_it_but_not_the_first_free_start(api: Api) -> None:
    w = await world(api)
    _, first = await customer(api)
    _, second = await customer(api)
    assert (await w.book(first, local(9))).status_code == 201
    assert code(await w.book(second, local(9, 30))) == "SLOT_TAKEN"  # inside the 10-minute buffer
    assert (await w.book(second, local(9, 45))).status_code == 201


async def test_the_database_itself_rejects_a_buffer_overlap(api: Api) -> None:
    w = await world(api)
    user, _ = await customer(api)
    await w.insert(user.id, utc(9), utc(9, 40))  # stored range with the buffer
    with pytest.raises(IntegrityError, match="no_overlap"):
        await w.insert(user.id, utc(9, 30), utc(10, 10))
    await w.insert(user.id, utc(9, 40), utc(10, 20))  # back to back is allowed


@pytest.mark.parametrize(("no_shows", "allowed"), [(2, True), (3, False), (7, False)])
async def test_booking_restriction_at_three_no_shows(
    api: Api, no_shows: int, allowed: bool
) -> None:
    w = await world(api)
    user, headers = await customer(api)
    async with api.sessions() as session:
        await session.execute(update(User).where(User.id == user.id).values(no_show_count=no_shows))
        await session.commit()
    response = await w.book(headers, local(9))
    if allowed:
        assert response.status_code == 201
    else:
        assert response.status_code == 403 and code(response) == "BOOKING_RESTRICTED"


async def test_cap_of_five_active_bookings(api: Api) -> None:
    w = await world(api)
    user, headers = await customer(api)
    ids = []
    for hour in range(9, 14):
        response = await w.book(headers, local(hour))
        assert response.status_code == 201
        ids.append(response.json()["id"])
    over = await w.book(headers, local(14))
    assert over.status_code == 409 and code(over) == "BOOKING_LIMIT_REACHED"
    await api.client.delete(f"{URL}/{ids[0]}", headers=headers)  # a cancelled one frees a place
    assert (await w.book(headers, local(14))).status_code == 201


async def test_ended_and_non_active_appointments_do_not_count_towards_the_cap(api: Api) -> None:
    w = await world(api)
    user, headers = await customer(api)
    old = CLOCK - timedelta(days=3)
    for hour in range(3):  # booked but already over
        begin = old + timedelta(hours=hour)
        await w.insert(user.id, begin, begin + timedelta(minutes=40))
    for status in ("completed", "cancelled", "no_show"):
        await w.insert(user.id, utc(9), utc(9, 40), status)
    for hour in range(9, 14):
        assert (await w.book(headers, local(hour))).status_code == 201


async def test_booking_rate_limit_and_default_is_checked_after_404(
    migrated_url: str, redis_url: str
) -> None:
    async with open_api(migrated_url, redis_url, {"booking_rate_limit_per_hour": 2}) as api:
        w = await world(api)
        _, headers = await customer(api)
        assert (await w.book(headers, local(9))).status_code == 201
        assert (await w.book(headers, local(10))).status_code == 201
        blocked = await w.book(headers, local(11))
        assert blocked.status_code == 429 and "Retry-After" in blocked.headers
        unknown = await api.client.post(
            URL, json=w.body(local(9), service_id=str(uuid.uuid4())), headers=headers
        )
        assert unknown.status_code == 404  # 404 comes before the rate limit
        _, other = await customer(api)
        assert (await w.book(other, local(12))).status_code == 201  # the limit is per user


async def test_same_idempotency_key_creates_one_appointment(api: Api) -> None:
    w = await world(api)
    user, headers = await customer(api)
    extra = {"Idempotency-Key": "book-1"}
    first = await w.book(headers, local(9), extra=extra)
    again = await w.book(headers, local(9), extra=extra)
    assert (first.status_code, again.status_code) == (201, 201)
    assert again.headers["Idempotent-Replayed"] == "true" and again.json() == first.json()
    async with api.sessions() as session:
        count = await session.scalar(
            select(func.count()).select_from(Appointment).where(Appointment.customer_id == user.id)
        )
    assert count == 1


async def test_a_cancelled_request_releases_the_idempotency_claim(api: Api) -> None:
    w = await world(api)
    user, headers = await customer(api)
    extra = {"Idempotency-Key": "stuck"}
    async with api.sessions() as holder:  # blocks the booking on the customer row lock
        await holder.execute(select(User.id).where(User.id == user.id).with_for_update())
        task = asyncio.create_task(w.book(headers, local(9), extra=extra))
        async with api.sessions() as probe:
            for _ in range(5000):  # poll the lock table; no sleeps
                waiting = await probe.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks "
                        "WHERE NOT granted AND locktype = 'transactionid'"
                    )
                )
                if waiting:
                    break
                await asyncio.sleep(0)
            else:
                pytest.fail("the booking never waited on the lock")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await holder.rollback()
    retry = await w.book(headers, local(9), extra=extra)
    assert retry.status_code == 201, retry.text  # not the in-progress 409


# --- cancel ---


async def book_one(w: World, headers: Headers, hour: int = 9) -> str:
    response = await w.book(headers, local(hour))
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def audit_rows(api: Api, appointment_id: str) -> list[AuditLog]:
    async with api.sessions() as session:
        return list(
            (
                await session.scalars(
                    select(AuditLog).where(AuditLog.entity_id == uuid.UUID(appointment_id))
                )
            ).all()
        )


async def status_of(api: Api, appointment_id: str) -> str:
    async with api.sessions() as session:
        found = await session.scalar(
            select(Appointment.status).where(Appointment.id == uuid.UUID(appointment_id))
        )
        assert found is not None
        return found


async def test_customer_cancels_own_without_an_audit_row_and_frees_the_slot(api: Api) -> None:
    w = await world(api)
    _, headers = await customer(api)
    appointment_id = await book_one(w, headers)
    _, other = await customer(api)
    assert code(await w.book(other, local(9))) == "SLOT_TAKEN"
    deleted = await api.client.delete(f"{URL}/{appointment_id}", headers=headers)
    assert deleted.status_code == 204 and deleted.content == b""
    assert await status_of(api, appointment_id) == "cancelled"
    assert await audit_rows(api, appointment_id) == []
    assert (await w.book(other, local(9))).status_code == 201  # book, cancel, book again


async def test_staff_and_owner_cancel_with_an_audit_row(api: Api) -> None:
    w = await world(api)
    staff = await w.staff()
    user, headers = await customer(api)
    first, second = await book_one(w, headers, 9), await book_one(w, headers, 10)
    assert (await api.client.delete(f"{URL}/{first}", headers=staff)).status_code == 204
    assert (await api.client.delete(f"{URL}/{second}", headers=w.owner)).status_code == 204
    for appointment_id in (first, second):
        (row,) = await audit_rows(api, appointment_id)
        assert (row.business_id, row.action, row.entity_type) == (
            w.business_id,
            "appointment.cancel",
            "appointment",
        )
        assert row.data == {"customer_id": str(user.id)}
    async with api.sessions() as session:
        actors = {r.actor_id for r in await session.scalars(select(AuditLog))}
    assert len(actors) >= 2


async def test_strangers_get_the_same_404_as_a_missing_id(api: Api) -> None:
    w = await world(api)
    other_business = await world(api)
    _, owner_customer = await customer(api)
    appointment_id = await book_one(w, owner_customer)
    _, stranger = await customer(api)
    other_staff = await other_business.staff()
    missing = await api.client.delete(f"{URL}/{uuid.uuid4()}", headers=stranger)
    assert missing.status_code == 404
    for headers in (stranger, other_staff, other_business.owner):
        response = await api.client.delete(f"{URL}/{appointment_id}", headers=headers)
        assert response.status_code == 404
        assert response.json() == missing.json()
    assert await status_of(api, appointment_id) == "booked"
    assert await audit_rows(api, appointment_id) == []


@pytest.mark.parametrize(
    "status", ["checked_in", "in_service", "completed", "no_show", "rescheduled"]
)
async def test_only_booked_can_be_cancelled(api: Api, status: str) -> None:
    w = await world(api)
    staff = await w.staff()
    user, headers = await customer(api)
    appointment_id = str(await w.insert(user.id, utc(9), utc(9, 40), status))
    for who in (headers, staff):
        response = await api.client.delete(f"{URL}/{appointment_id}", headers=who)
        assert response.status_code == 409 and code(response) == "INVALID_TRANSITION"
    assert await status_of(api, appointment_id) == status
    assert await audit_rows(api, appointment_id) == []  # a failed cancel writes no audit row


async def test_a_started_appointment_cannot_be_cancelled_by_anyone(api: Api) -> None:
    w = await world(api)
    staff = await w.staff()
    _, headers = await customer(api)
    appointment_id = await book_one(w, headers)
    api.clock.now = utc(9, 10).timestamp()
    for who in (headers, staff, w.owner):
        response = await api.client.delete(f"{URL}/{appointment_id}", headers=who)
        assert response.status_code == 409 and code(response) == "INVALID_TRANSITION"
    assert await status_of(api, appointment_id) == "booked"
    assert await audit_rows(api, appointment_id) == []


async def test_cancelling_twice_is_204_and_the_second_call_is_not_audited(api: Api) -> None:
    w = await world(api)
    staff = await w.staff()
    _, headers = await customer(api)
    appointment_id = await book_one(w, headers)
    for _ in range(2):
        assert (
            await api.client.delete(f"{URL}/{appointment_id}", headers=staff)
        ).status_code == 204
    assert len(await audit_rows(api, appointment_id)) == 1
    assert (await api.client.delete(f"{URL}/{appointment_id}", headers=headers)).status_code == 204


async def test_cancel_roles_and_malformed_id(api: Api) -> None:
    w = await world(api)
    _, headers = await customer(api)
    appointment_id = await book_one(w, headers)
    assert (await api.client.delete(f"{URL}/{appointment_id}")).status_code == 401
    assert (
        await api.client.delete(f"{URL}/{appointment_id}", headers=await admin(api))
    ).status_code == 403
    assert (await api.client.delete(f"{URL}/not-a-uuid", headers=headers)).status_code == 422


# --- list ---


async def seed(w: World, user: User, days: int = 5) -> list[str]:
    """Appointments on distinct start times, inserted directly; returns their ids by start."""
    ids = []
    for index in range(days):
        start = utc(9) + timedelta(hours=index)
        ids.append(str(await w.insert(user.id, start, start + timedelta(minutes=40))))
    return ids


async def get_list(api: Api, headers: Headers, **params: str | int) -> Any:
    return await api.client.get(URL, params=params, headers=headers)


async def test_customer_sees_only_own_and_staff_fields_are_minimal(api: Api) -> None:
    w = await world(api)
    mine, my_headers = await customer(api)
    _, theirs = await customer(api)
    other, _ = await customer(api)
    own = await seed(w, mine, 2)
    await w.insert(other.id, utc(15), utc(15, 40))
    page = (await get_list(api, my_headers)).json()
    assert [item["id"] for item in page["items"]] == own
    assert page["next_cursor"] is None and "customer" not in page["items"][0]
    assert (await get_list(api, theirs)).json()["items"] == []
    staff = await w.staff()
    seen = (await get_list(api, staff, business_id=str(w.business_id))).json()["items"]
    assert len(seen) == 3
    assert set(seen[0]) - set(page["items"][0]) == {"customer"}
    assert set(seen[0]["customer"]) == {"id", "name"}
    assert (await get_list(api, w.owner, business_id=str(w.business_id))).json()["items"] == seen


async def test_filters_status_from_and_to_boundaries(api: Api) -> None:
    w = await world(api)
    user, headers = await customer(api)
    ids = await seed(w, user, 3)  # 09:00, 10:00, 11:00 local-equivalent UTC starts
    await w.insert(user.id, utc(14), utc(14, 40), "cancelled")
    starts = [utc(9) + timedelta(hours=i) for i in range(3)]
    iso = [start.isoformat() for start in starts]
    got = (await get_list(api, headers, status="cancelled")).json()["items"]
    assert [item["status"] for item in got] == ["cancelled"]
    window = (await get_list(api, headers, **{"from": iso[1], "to": iso[2]})).json()
    assert [item["id"] for item in window["items"]] == [ids[1]]  # from inclusive, to exclusive
    only_from = await get_list(api, headers, **{"from": iso[2]})
    assert [item["id"] for item in only_from.json()["items"]][0] == ids[2]


async def test_pagination_is_stable_across_pages(api: Api) -> None:
    w = await world(api)
    user, headers = await customer(api)
    ids = await seed(w, user, 5)
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(3):
        params: dict[str, str | int] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = (await get_list(api, headers, **params)).json()
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
    assert seen == ids and cursor is None


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 51},
        {"status": "nope"},
        {"from": "2026-11-02T09:00:00"},  # naive
        {"from": "2026-11-02T10:00:00+00:00", "to": "2026-11-02T09:00:00+00:00"},
        {"cursor": "not base64!"},
        {"cursor": "W10="},  # valid base64 of []
    ],
)
async def test_bad_list_parameters_are_422(api: Api, params: dict[str, str | int]) -> None:
    _, headers = await customer(api)
    response = await get_list(api, headers, **params)
    assert response.status_code == 422 and code(response) == "VALIDATION_ERROR"


async def test_cursor_with_a_bad_timestamp_is_422_on_cursor(api: Api) -> None:
    from app.schemas.business import encode_cursor

    _, headers = await customer(api)
    for raw in ("garbage", "2026-11-02T09:00:00"):  # not ISO, and naive
        response = await get_list(api, headers, cursor=encode_cursor(raw, uuid.uuid4()))
        assert response.status_code == 422
        assert response.json()["errors"][0]["loc"] == ["query", "cursor"]


async def test_limit_bounds_and_default(api: Api) -> None:
    w = await world(api)
    user, headers = await customer(api)
    await seed(w, user, 5)
    assert len((await get_list(api, headers, limit=50)).json()["items"]) == 5
    assert len((await get_list(api, headers, limit=1)).json()["items"]) == 1


async def test_staff_list_authorization(api: Api) -> None:
    w = await world(api)
    other = await world(api)
    staff, other_staff = await w.staff(), await other.staff()
    _, customer_headers = await customer(api)
    business = {"business_id": str(w.business_id)}
    assert (await get_list(api, staff)).status_code == 422  # required
    assert (await get_list(api, staff, business_id="nope")).status_code == 422
    assert (await get_list(api, staff, business_id=str(uuid.uuid4()))).status_code == 404
    assert (await get_list(api, other_staff, **business)).status_code == 403
    assert (await get_list(api, other.owner, **business)).status_code == 403
    assert (await get_list(api, customer_headers, **business)).status_code == 422
    assert (await get_list(api, await admin(api), **business)).status_code == 403
    assert (await api.client.get(URL)).status_code == 401
    assert (await get_list(api, staff, **business)).status_code == 200


async def test_displayed_end_follows_the_services_current_duration(api: Api) -> None:
    w = await world(api)
    user, headers = await customer(api)
    await seed(w, user, 1)
    await api.client.patch(
        f"/v1/businesses/{w.business_id}/services/{w.service_id}",
        json={"duration_min": 60},
        headers=w.owner,
    )
    item = (await get_list(api, headers)).json()["items"][0]
    assert item["end"] == local(10)  # known limitation: no stored duration column


async def test_list_uses_one_query_regardless_of_rows(api: Api) -> None:
    w = await world(api)
    user, headers = await customer(api)
    await seed(w, user, 5)
    statements: list[str] = []
    engine = api.app.state.engine.sync_engine

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = await get_list(api, headers)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(response.json()["items"]) == 5
    assert sum("FROM appointments" in s for s in statements) == 1
