import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.dialects.postgresql import Range

from app.models import Appointment, Business, Resource
from tests.integration.api import Api, new_phone
from tests.integration.test_business_admin import PASSWORD, login, new_staff
from tests.integration.test_businesses import new_business, new_service, user_headers

pytestmark = pytest.mark.anyio

Headers = dict[str, str]
# 2026-11-02 is a Monday. The injected clock sits the day before, in every zone used here.
CLOCK = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
MONDAY = "2026-11-02"


def hours(*spans: tuple[str, str]) -> dict[str, list[dict[str, str]]]:
    return {"mon": [{"open": o, "close": c} for o, c in spans]}


async def setup_business(
    api: Api,
    *,
    zone: str = "Asia/Kolkata",
    open_hours: dict[str, list[dict[str, str]]] | None = None,
    service: dict[str, object] | None = None,
    clock: datetime = CLOCK,
) -> tuple[Headers, uuid.UUID, str]:
    api.clock.now = clock.timestamp()
    _, owner = await user_headers(api, "owner")
    business_id = await new_business(
        api, owner, timezone=zone, open_hours=open_hours or hours(("09:00", "12:00"))
    )
    service_id = await new_service(api, business_id, owner, **(service or {}))
    return owner, business_id, service_id


async def get_slots(
    api: Api,
    headers: Headers,
    business_id: uuid.UUID,
    service_id: str,
    day: str = MONDAY,
) -> dict[str, Any]:
    response = await api.client.get(
        f"/v1/businesses/{business_id}/availability",
        params={"service_id": service_id, "date": day},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def starts(body: dict[str, Any]) -> list[str]:
    return [slot["start"] for slot in body["slots"]]


async def resource_id(api: Api, business_id: uuid.UUID) -> uuid.UUID:
    async with api.sessions() as session:
        found = await session.scalar(select(Resource.id).where(Resource.business_id == business_id))
        assert found is not None
        return found


async def book(
    api: Api,
    business_id: uuid.UUID,
    service_id: str,
    resource: uuid.UUID,
    start: datetime,
    end: datetime,
    status: str = "booked",
) -> None:
    customer = await api.create_user("customer", phone=new_phone())
    async with api.sessions() as session:
        session.add(
            Appointment(
                business_id=business_id,
                service_id=uuid.UUID(service_id),
                resource_id=resource,
                customer_id=customer.id,
                during=Range(start, end, bounds="[)"),
                status=status,
            )
        )
        await session.commit()


def utc(local_hour: int, local_minute: int = 0, day: int = 2, offset_min: int = 330) -> datetime:
    """A Kolkata wall time as UTC."""
    local = datetime(2026, 11, day, local_hour, local_minute, tzinfo=UTC)
    return local - timedelta(minutes=offset_min)


async def test_slots_in_business_time_with_offset_and_visible_end(api: Api) -> None:
    owner, business_id, service_id = await setup_business(api, service={"duration_min": 30})
    body = await get_slots(api, owner, business_id, service_id)
    assert body["timezone"] == "Asia/Kolkata"
    slots = body["slots"]
    assert len(slots) == 11  # 09:00 to 11:30 on a 15-minute grid
    assert slots[0] == {
        "start": "2026-11-02T09:00:00+05:30",
        "end": "2026-11-02T09:30:00+05:30",
        "resource_id": str(await resource_id(api, business_id)),
    }


async def test_any_authenticated_role_can_read_availability(api: Api) -> None:
    owner, business_id, service_id = await setup_business(api)
    _, customer = await user_headers(api, "customer")
    api.clock.now = time.time()  # login stamps tokens with the app clock; JWT checks use real time
    staff = await new_staff(api, owner, business_id)
    staff_headers = await login(api, staff["email"], PASSWORD)
    api.clock.now = CLOCK.timestamp()
    for headers in (customer, staff_headers, owner):
        assert starts(await get_slots(api, headers, business_id, service_id))


async def test_only_active_appointments_block_slots(api: Api) -> None:
    owner, business_id, service_id = await setup_business(api, service={"duration_min": 30})
    resource = await resource_id(api, business_id)
    active = [("booked", 9, 0), ("checked_in", 9, 30), ("in_service", 10, 0)]
    inactive = [("cancelled", 10, 30), ("completed", 11, 0), ("no_show", 11, 30)]
    for status, hour, minute in active + inactive + [("rescheduled", 9, 0)]:
        end = utc(hour, minute + 30) if minute == 0 else utc(hour + 1, 0)
        await book(api, business_id, service_id, resource, utc(hour, minute), end, status)
    body = await get_slots(api, owner, business_id, service_id)
    assert starts(body) == [
        f"2026-11-02T{t}:00+05:30" for t in ("10:30", "10:45", "11:00", "11:15", "11:30")
    ]


async def test_other_resource_and_other_business_do_not_block(api: Api) -> None:
    owner, business_id, service_id = await setup_business(api, service={"duration_min": 30})
    other_owner, other_business, other_service = await setup_business(
        api, service={"duration_min": 30}
    )
    async with api.sessions() as session:
        extra = Resource(business_id=business_id, name="Zed")
        session.add(extra)
        await session.commit()
    await book(api, business_id, service_id, extra.id, utc(9), utc(12))
    await book(
        api, other_business, other_service, await resource_id(api, other_business), utc(9), utc(12)
    )
    assert len(starts(await get_slots(api, owner, business_id, service_id))) == 11
    assert other_owner


async def test_buffer_is_part_of_the_stored_range_but_not_of_opening_hours(api: Api) -> None:
    owner, business_id, service_id = await setup_business(
        api,
        open_hours=hours(("09:00", "17:00")),
        service={"duration_min": 30, "buffer_min": 10},
    )
    resource = await resource_id(api, business_id)
    await book(api, business_id, service_id, resource, utc(11), utc(11, 40))
    found = {
        s.split("T")[1][:5] for s in starts(await get_slots(api, owner, business_id, service_id))
    }
    assert "10:15" in found  # stored range ends 10:55
    assert "10:30" not in found  # its buffer would overlap the 11:00 appointment
    assert "11:30" not in found  # inside the booked appointment's buffer
    assert "11:45" in found  # offered once that buffer has ended
    assert "16:30" in found  # service ends at 17:00; only the buffer runs past closing
    assert "16:45" not in found


async def test_2359_close_means_end_of_day(api: Api) -> None:
    owner, business_id, service_id = await setup_business(
        api, open_hours=hours(("22:00", "23:59")), service={"duration_min": 30}
    )
    body = await get_slots(api, owner, business_id, service_id)
    assert body["slots"][-1]["start"] == "2026-11-02T23:30:00+05:30"
    assert body["slots"][-1]["end"] == "2026-11-03T00:00:00+05:30"


async def test_dst_ordinary_days_new_york(api: Api) -> None:
    for day, first, clock in (
        ("2026-03-08", "2026-03-08T09:00:00-04:00", datetime(2026, 3, 1, tzinfo=UTC)),
        ("2026-11-01", "2026-11-01T09:00:00-05:00", datetime(2026, 10, 25, tzinfo=UTC)),
    ):
        weekday = "sun"
        owner, business_id, service_id = await setup_business(
            api,
            zone="America/New_York",
            open_hours={weekday: [{"open": "09:00", "close": "17:00"}]},
            service={"duration_min": 30},
            clock=clock,
        )
        slots = starts(await get_slots(api, owner, business_id, service_id, day))
        assert slots[0] == first
        assert len(slots) == 31


async def test_dst_overnight_interval_spring_forward(api: Api) -> None:
    owner, business_id, service_id = await setup_business(
        api,
        zone="America/New_York",
        open_hours={"sun": [{"open": "00:00", "close": "06:00"}]},
        service={"duration_min": 60},
        clock=datetime(2026, 3, 1, tzinfo=UTC),
    )
    body = await get_slots(api, owner, business_id, service_id, "2026-03-08")
    found = starts(body)
    assert len(found) == 17  # 00:00-01:45 EST (8) and 03:00-05:00 EDT (9); the 02:xx gap is skipped
    assert not any("T02:" in s for s in found)
    assert "2026-03-08T03:00:00-04:00" in found
    assert found[0] == "2026-03-08T00:00:00-05:00"


async def test_dst_overnight_interval_fall_back(api: Api) -> None:
    owner, business_id, service_id = await setup_business(
        api,
        zone="America/New_York",
        open_hours={"sun": [{"open": "00:00", "close": "06:00"}]},
        service={"duration_min": 60},
        clock=datetime(2026, 10, 25, tzinfo=UTC),
    )
    found = starts(await get_slots(api, owner, business_id, service_id, "2026-11-01"))
    assert len(found) == 21 and len(set(found)) == 21  # each wall time once
    assert "2026-11-01T01:15:00-04:00" in found  # first pass of the repeated hour only
    assert not any(s.startswith("2026-11-01T01:") and s.endswith("-05:00") for s in found)
    assert "2026-11-01T02:00:00-05:00" in found


async def test_checks_run_in_order_401_404_then_422(api: Api) -> None:
    owner, business_id, service_id = await setup_business(api)
    url = f"/v1/businesses/{business_id}/availability"
    for params in (
        {"service_id": service_id, "date": "garbage"},
        {"service_id": "x", "date": "no"},
    ):
        assert (await api.client.get(url, params=params)).status_code == 401
    malformed = await api.client.get(url, params={"service_id": "x", "date": "no"}, headers=owner)
    assert malformed.status_code == 404  # a malformed service id cannot match
    missing = await api.client.get(f"/v1/businesses/{uuid.uuid4()}/availability", headers=owner)
    assert missing.status_code == 404
    bad_date = await api.client.get(
        url, params={"service_id": service_id, "date": "garbage"}, headers=owner
    )
    assert bad_date.status_code == 422
    assert bad_date.json()["errors"][0]["loc"] == ["query", "date"]


@pytest.mark.parametrize("day", ["2026-10-31", "2027-01-02", "2026-11-2", ""])
async def test_out_of_range_dates_are_422(api: Api, day: str) -> None:
    owner, business_id, service_id = await setup_business(api)
    response = await api.client.get(
        f"/v1/businesses/{business_id}/availability",
        params={"service_id": service_id, "date": day},
        headers=owner,
    )
    assert response.status_code == 422


async def test_today_and_sixty_days_ahead_are_accepted(api: Api) -> None:
    owner, business_id, service_id = await setup_business(api)
    for day in ("2026-11-01", "2026-12-31"):
        await get_slots(api, owner, business_id, service_id, day)


async def test_inactive_foreign_and_unknown_services_are_404(api: Api) -> None:
    owner, business_id, service_id = await setup_business(api)
    inactive = await new_service(api, business_id, owner, name="Old", is_active=False)
    other_owner, other_business, foreign = await setup_business(api)
    url = f"/v1/businesses/{business_id}/availability"
    for target in (inactive, foreign, str(uuid.uuid4())):
        response = await api.client.get(
            url, params={"service_id": target, "date": MONDAY}, headers=owner
        )
        assert response.status_code == 404, target
    assert other_owner and other_business and service_id


async def test_no_active_resource_is_404_and_several_are_picked_by_name(api: Api) -> None:
    owner, business_id, service_id = await setup_business(api)
    async with api.sessions() as session:
        alpha = Resource(business_id=business_id, name="Alpha")
        session.add(alpha)
        await session.commit()
    body = await get_slots(api, owner, business_id, service_id)
    assert body["slots"][0]["resource_id"] == str(alpha.id)
    async with api.sessions() as session:
        await session.execute(
            update(Resource).where(Resource.business_id == business_id).values(is_active=False)
        )
        await session.commit()
    response = await api.client.get(
        f"/v1/businesses/{business_id}/availability",
        params={"service_id": service_id, "date": MONDAY},
        headers=owner,
    )
    assert response.status_code == 404


async def test_rate_limit_is_120_per_minute_per_user(api: Api) -> None:
    owner, business_id, service_id = await setup_business(api)
    for _ in range(120):
        await get_slots(api, owner, business_id, service_id)
    blocked = await api.client.get(
        f"/v1/businesses/{business_id}/availability",
        params={"service_id": service_id, "date": MONDAY},
        headers=owner,
    )
    assert blocked.status_code == 429 and "Retry-After" in blocked.headers
    _, other = await user_headers(api, "customer")
    await get_slots(api, other, business_id, service_id)


async def test_one_query_for_the_days_busy_ranges(api: Api) -> None:
    owner, business_id, service_id = await setup_business(api)
    resource = await resource_id(api, business_id)
    for hour in (9, 10, 11):
        await book(api, business_id, service_id, resource, utc(hour), utc(hour, 30))
    statements: list[str] = []
    engine = api.app.state.engine.sync_engine

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        body = await get_slots(api, owner, business_id, service_id)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert sum("FROM appointments" in s for s in statements) == 1
    assert len(body["slots"]) > 0


async def test_timezone_change_is_blocked_while_appointments_are_active(api: Api) -> None:
    owner, business_id, service_id = await setup_business(api)
    resource = await resource_id(api, business_id)
    url = f"/v1/businesses/{business_id}"
    soon = CLOCK + timedelta(days=1)
    for status in ("cancelled", "completed", "no_show", "rescheduled"):
        await book(api, business_id, service_id, resource, soon, soon + timedelta(hours=1), status)
    await book(
        api, business_id, service_id, resource, CLOCK - timedelta(days=2), CLOCK - timedelta(days=1)
    )
    allowed = await api.client.patch(url, json={"timezone": "Europe/London"}, headers=owner)
    assert allowed.status_code == 200  # only past or inactive appointments exist
    await api.client.patch(url, json={"timezone": "Asia/Kolkata"}, headers=owner)
    for status in ("booked", "checked_in", "in_service"):
        async with api.sessions() as session:
            await session.execute(
                update(Appointment).where(Appointment.status == "booked").values(status="cancelled")
            )
            await session.commit()
        await book(api, business_id, service_id, resource, soon, soon + timedelta(hours=1), status)
        blocked = await api.client.patch(url, json={"timezone": "Europe/London"}, headers=owner)
        assert blocked.status_code == 422, status
        assert blocked.json()["errors"][0]["loc"] == ["body", "timezone"]
        async with api.sessions() as session:
            await session.execute(
                update(Appointment).where(Appointment.status == status).values(status="cancelled")
            )
            await session.commit()
    same = await api.client.patch(url, json={"timezone": "Asia/Kolkata"}, headers=owner)
    assert same.status_code == 200
    async with api.sessions() as session:
        business = await session.get(Business, business_id)
        assert business is not None and business.timezone == "Asia/Kolkata"


async def test_other_edits_are_not_blocked_by_active_appointments(api: Api) -> None:
    owner, business_id, service_id = await setup_business(api)
    resource = await resource_id(api, business_id)
    await book(
        api,
        business_id,
        service_id,
        resource,
        CLOCK + timedelta(days=1),
        CLOCK + timedelta(days=1, hours=1),
    )
    response = await api.client.patch(
        f"/v1/businesses/{business_id}", json={"name": "Renamed"}, headers=owner
    )
    assert response.status_code == 200
