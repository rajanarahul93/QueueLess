import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update

from app.models import Appointment, User
from tests.integration.api import Api, open_api
from tests.integration.test_availability import Headers, utc
from tests.integration.test_booking import (
    URL,
    World,
    admin,
    book_one,
    code,
    customer,
    local,
    status_of,
    world,
)

pytestmark = pytest.mark.anyio


async def move(w: World, headers: Headers, appointment_id: str, start: str, **extra: str) -> Any:
    return await w.api.client.patch(
        f"{URL}/{appointment_id}/reschedule", json={"start": start}, headers=headers | extra
    )


async def row(api: Api, appointment_id: str) -> Appointment:
    async with api.sessions() as session:
        found = await session.get(Appointment, uuid.UUID(appointment_id))
        assert found is not None
        return found


def snapshot(appointment: Appointment) -> tuple[object, ...]:
    return (
        appointment.status,
        appointment.during,
        appointment.rescheduled_from_id,
        appointment.created_at,
        appointment.service_id,
        appointment.resource_id,
    )


async def test_happy_path_moves_and_links(api: Api) -> None:
    w = await world(api)
    _, headers = await customer(api)
    old_id = await book_one(w, headers)
    response = await move(w, headers, old_id, local(11))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rescheduled_from_id"] == old_id
    assert (body["start"], body["end"], body["status"]) == (local(11), local(11, 30), "booked")
    assert body["service_id"] == w.service_id and body["resource_id"] == str(w.resource)
    new, old = await row(api, body["id"]), await row(api, old_id)
    assert new.rescheduled_from_id == uuid.UUID(old_id)
    assert (new.during.lower, new.during.upper) == (utc(11), utc(11, 40))  # buffer stored
    assert old.status == "rescheduled" and old.rescheduled_from_id is None
    assert (old.during.lower, old.during.upper) == (utc(9), utc(9, 40))


@pytest.mark.parametrize(
    "target",
    [local(9, 15), local(9, 30), local(9, day=9)],  # overlapping shift, by the duration, new day
)
async def test_moves_that_overlap_the_old_range_or_change_day(api: Api, target: str) -> None:
    w = await world(api)
    _, headers = await customer(api)
    old_id = await book_one(w, headers)
    response = await move(w, headers, old_id, target)
    assert response.status_code == 200, response.text
    assert response.json()["start"] == target


async def test_taken_slot_leaves_the_old_appointment_exactly_unchanged(api: Api) -> None:
    w = await world(api)
    user, headers = await customer(api)
    _, other = await customer(api)
    old_id = await book_one(w, headers, 9)
    await book_one(w, other, 11)
    before = snapshot(await row(api, old_id))
    response = await move(w, headers, old_id, local(11))
    assert response.status_code == 409 and code(response) == "SLOT_TAKEN"
    starts = [slot["start"] for slot in response.json()["next_slots"]]
    assert starts == [local(11, 45), local(12), local(12, 15)]
    assert local(11) not in starts
    assert snapshot(await row(api, old_id)) == before
    async with api.sessions() as session:
        mine = await session.scalar(
            select(func.count()).select_from(Appointment).where(Appointment.customer_id == user.id)
        )
    assert mine == 1  # nothing was inserted


async def test_next_slots_leave_out_the_customers_own_old_range(api: Api) -> None:
    w = await world(api)
    _, headers = await customer(api)
    _, other = await customer(api)
    old_id = await book_one(w, headers, 10, minute=30)  # stored [10:30, 11:10)
    await book_one(w, other, 9, minute=30)  # stored [09:30, 10:10)
    response = await move(w, headers, old_id, local(9, 30))
    assert code(response) == "SLOT_TAKEN"
    starts = [slot["start"] for slot in response.json()["next_slots"]]
    # 10:15 and 10:30 overlap the customer's own old range, which a reschedule may reuse.
    assert starts == [local(10, 15), local(10, 30), local(10, 45)]
    assert local(9, 30) not in starts


async def test_old_row_is_terminal_and_frees_its_slot(api: Api) -> None:
    w = await world(api)
    _, headers = await customer(api)
    _, other = await customer(api)
    old_id = await book_one(w, headers, 9)
    assert (await move(w, headers, old_id, local(11))).status_code == 200
    assert (await w.book(other, local(9))).status_code == 201  # the old range is free
    cancel = await api.client.delete(f"{URL}/{old_id}", headers=headers)
    assert cancel.status_code == 409 and code(cancel) == "INVALID_TRANSITION"
    again = await move(w, headers, old_id, local(13))
    assert again.status_code == 409 and code(again) == "INVALID_TRANSITION"
    listed = await api.client.get(URL, params={"status": "rescheduled"}, headers=headers)
    assert [item["id"] for item in listed.json()["items"]] == [old_id]


async def test_a_chain_keeps_every_link_and_only_the_last_is_active(api: Api) -> None:
    w = await world(api)
    _, headers = await customer(api)
    first = await book_one(w, headers, 9)
    second = (await move(w, headers, first, local(10))).json()["id"]
    third = (await move(w, headers, second, local(11))).json()["id"]
    a, b, c = await row(api, first), await row(api, second), await row(api, third)
    assert a.rescheduled_from_id is None
    assert b.rescheduled_from_id == a.id and c.rescheduled_from_id == b.id
    assert (a.status, b.status, c.status) == ("rescheduled", "rescheduled", "booked")


@pytest.mark.parametrize(
    "status", ["checked_in", "in_service", "completed", "cancelled", "no_show", "rescheduled"]
)
async def test_only_booked_can_be_rescheduled(api: Api, status: str) -> None:
    w = await world(api)
    user, headers = await customer(api)
    old_id = str(await w.insert(user.id, utc(9), utc(9, 40), status))
    response = await move(w, headers, old_id, local(11))
    assert response.status_code == 409 and code(response) == "INVALID_TRANSITION"
    assert await status_of(api, old_id) == status


async def test_state_is_checked_before_the_cutoff(api: Api) -> None:
    w = await world(api)
    user, headers = await customer(api)
    old_id = str(await w.insert(user.id, utc(9), utc(9, 40), "cancelled"))
    api.clock.now = (utc(9) - timedelta(minutes=5)).timestamp()  # inside the cutoff too
    assert code(await move(w, headers, old_id, local(11))) == "INVALID_TRANSITION"


async def test_started_appointment_cannot_be_rescheduled(api: Api) -> None:
    w = await world(api)
    _, headers = await customer(api)
    old_id = await book_one(w, headers, 9)
    api.clock.now = utc(9, 10).timestamp()
    response = await move(w, headers, old_id, local(11))
    assert response.status_code == 409 and code(response) == "INVALID_TRANSITION"
    assert await status_of(api, old_id) == "booked"


@pytest.mark.parametrize(("minutes_before", "allowed"), [(120, True), (119, False)])
async def test_cutoff_boundary(api: Api, minutes_before: int, allowed: bool) -> None:
    w = await world(api)
    _, headers = await customer(api)
    old_id = await book_one(w, headers, 9)
    api.clock.now = (utc(9) - timedelta(minutes=minutes_before)).timestamp()
    response = await move(w, headers, old_id, local(11))
    if allowed:
        assert response.status_code == 200, response.text  # exactly at the cutoff is allowed
    else:
        assert response.status_code == 409 and code(response) == "RESCHEDULE_CUTOFF"
        assert await status_of(api, old_id) == "booked"


async def test_same_time_is_422_and_marks_nothing(api: Api) -> None:
    w = await world(api)
    _, headers = await customer(api)
    old_id = await book_one(w, headers, 9)
    before = snapshot(await row(api, old_id))
    response = await move(w, headers, old_id, local(9))
    assert response.status_code == 422
    assert response.json()["errors"] == [{"loc": ["body", "start"], "msg": "same time"}]
    assert snapshot(await row(api, old_id)) == before


@pytest.mark.parametrize(
    "start",
    [
        "2026-10-31T09:00:00+05:30",  # past
        "2027-01-15T09:00:00+05:30",  # too far
        local(9, 7),  # off the grid
        local(8, 45),  # before opening
        local(16, 45),  # does not fit
        local(9, day=3),  # closed day
        "2026-11-02T11:00:00",  # naive
    ],
)
async def test_new_start_follows_the_booking_rules(api: Api, start: str) -> None:
    w = await world(api)
    _, headers = await customer(api)
    old_id = await book_one(w, headers, 9)
    response = await move(w, headers, old_id, start)
    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["loc"] == ["body", "start"]
    assert await status_of(api, old_id) == "booked"


async def test_body_cannot_change_service_or_resource(api: Api) -> None:
    w = await world(api)
    _, headers = await customer(api)
    old_id = await book_one(w, headers, 9)
    response = await api.client.patch(
        f"{URL}/{old_id}/reschedule",
        json={"start": local(11), "resource_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert response.status_code == 422


async def test_roles_and_strangers(api: Api) -> None:
    w = await world(api)
    _, headers = await customer(api)
    _, stranger = await customer(api)
    old_id = await book_one(w, headers, 9)
    for role_headers in (w.owner, await w.staff(), await admin(api)):
        assert (await move(w, role_headers, old_id, local(11))).status_code == 403
    assert (
        await api.client.patch(f"{URL}/{old_id}/reschedule", json={"start": local(11)})
    ).status_code == 401
    missing = await move(w, stranger, str(uuid.uuid4()), local(11))
    theirs = await move(w, stranger, old_id, local(11))
    assert missing.status_code == theirs.status_code == 404
    assert missing.json() == theirs.json()
    assert await status_of(api, old_id) == "booked"


async def test_customer_at_the_cap_can_reschedule_but_not_book_a_sixth(api: Api) -> None:
    w = await world(api)
    _, headers = await customer(api)
    ids = [await book_one(w, headers, hour) for hour in range(9, 14)]
    assert (await move(w, headers, ids[0], local(15))).status_code == 200  # net zero
    over = await w.book(headers, local(16))
    assert over.status_code == 409 and code(over) == "BOOKING_LIMIT_REACHED"


async def test_restricted_customer_cannot_reschedule(api: Api) -> None:
    w = await world(api)
    user, headers = await customer(api)
    old_id = await book_one(w, headers, 9)
    async with api.sessions() as session:
        await session.execute(update(User).where(User.id == user.id).values(no_show_count=3))
        await session.commit()
    response = await move(w, headers, old_id, local(11))
    assert response.status_code == 403 and code(response) == "BOOKING_RESTRICTED"
    assert await status_of(api, old_id) == "booked"


async def test_idempotent_replay_returns_the_same_body(api: Api) -> None:
    w = await world(api)
    user, headers = await customer(api)
    old_id = await book_one(w, headers, 9)
    first = await move(w, headers, old_id, local(11), **{"Idempotency-Key": "move-1"})
    again = await move(w, headers, old_id, local(11), **{"Idempotency-Key": "move-1"})
    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json() == first.json() and again.headers["Idempotent-Replayed"] == "true"
    async with api.sessions() as session:
        rows = await session.scalar(
            select(func.count()).select_from(Appointment).where(Appointment.customer_id == user.id)
        )
    assert rows == 2  # the old one and exactly one new one


async def test_failed_reschedules_still_cost_a_booking_rate_limit_hit(
    migrated_url: str, redis_url: str
) -> None:
    async with open_api(migrated_url, redis_url, {"booking_rate_limit_per_hour": 3}) as api:
        w = await world(api)
        _, headers = await customer(api)
        old_id = await book_one(w, headers, 9)  # hit 1 (the shared booking bucket)
        assert (await move(w, headers, old_id, local(9))).status_code == 422  # hit 2
        cancelled = str(
            await w.insert((await row(api, old_id)).customer_id, utc(14), utc(14, 40), "cancelled")
        )
        assert (await move(w, headers, cancelled, local(11))).status_code == 409  # hit 3
        blocked = await w.book(headers, local(12))
        assert blocked.status_code == 429 and "Retry-After" in blocked.headers
