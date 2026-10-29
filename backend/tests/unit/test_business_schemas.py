import base64
import json
import uuid

import pytest
from pydantic import ValidationError

from app.core.errors import ValidationFailed
from app.schemas.business import (
    BusinessCreate,
    BusinessUpdate,
    ServiceUpdate,
    SettingsPatch,
    decode_cursor,
    encode_cursor,
)


def business(**overrides: object) -> dict[str, object]:
    return {
        "name": "Clinic",
        "category": "clinic",
        "timezone": "Asia/Kolkata",
        "open_hours": {"mon": [{"open": "09:00", "close": "17:00"}]},
    } | overrides


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode()


def test_cursor_round_trips() -> None:
    business_id = uuid.uuid4()
    assert decode_cursor(encode_cursor("Café_%", business_id)) == ("Café_%", business_id)


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64!",  # garbage
        "abc",  # bad padding
        b64(b"\xff\xfe"),  # not UTF-8
        b64(b"{not json"),  # not JSON
        b64(json.dumps({"name": "a", "id": str(uuid.uuid4())}).encode()),  # wrong shape
        b64(json.dumps(["a"]).encode()),  # too short
        b64(json.dumps(["a", str(uuid.uuid4()), "x"]).encode()),  # too long
        b64(json.dumps([1, str(uuid.uuid4())]).encode()),  # wrong type
        b64(json.dumps(["a", 5]).encode()),  # wrong type
        b64(json.dumps(["a", "not-a-uuid"]).encode()),  # invalid UUID
    ],
)
def test_bad_cursor_is_a_validation_error(cursor: str) -> None:
    with pytest.raises(ValidationFailed) as error:
        decode_cursor(cursor)
    assert error.value.extra["errors"] == [{"loc": ["query", "cursor"], "msg": "Invalid cursor"}]


def test_known_timezone_is_accepted() -> None:
    assert BusinessCreate.model_validate(business()).timezone == "Asia/Kolkata"


@pytest.mark.parametrize(
    "timezone",
    ["Mars/Base", "localtime", "posixrules", "posix/UTC", "right/UTC", "../etc/passwd", ""],
)
def test_unknown_timezone_is_rejected(timezone: str) -> None:
    with pytest.raises(ValidationError):
        BusinessCreate.model_validate(business(timezone=timezone))


@pytest.mark.parametrize(
    "open_hours",
    [
        {"mon": [{"open": "17:00", "close": "09:00"}]},
        {"mon": [{"open": "09:00", "close": "09:00"}]},
        {"mon": [{"open": "9:00", "close": "17:00"}]},
        {"funday": [{"open": "09:00", "close": "17:00"}]},
    ],
)
def test_bad_open_hours_are_rejected(open_hours: object) -> None:
    with pytest.raises(ValidationError):
        BusinessCreate.model_validate(business(open_hours=open_hours))


def test_service_update_rejects_explicit_null_but_allows_omission() -> None:
    assert ServiceUpdate.model_validate({}).model_fields_set == set()
    with pytest.raises(ValidationError, match="cannot be null"):
        ServiceUpdate.model_validate({"price": None})


SETTINGS_BOUNDS = {
    "grace_period_min": (0, 60),
    "reschedule_cutoff_min": (0, 10080),
    "promotion_wait_limit_min": (1, 240),
    "running_late_threshold_min": (1, 120),
    "max_walk_ins": (0, 1000),
}


@pytest.mark.parametrize("key", SETTINGS_BOUNDS)
def test_settings_bounds_at_and_beyond_the_edge(key: str) -> None:
    low, high = SETTINGS_BOUNDS[key]
    for ok in (low, high):
        assert SettingsPatch.model_validate({key: ok}).model_fields_set == {key}
    for bad in (low - 1, high + 1, True, 1.5, "5", None):
        with pytest.raises(ValidationError):
            SettingsPatch.model_validate({key: bad})


def test_walk_in_ratio_bounds_and_both_keys_required() -> None:
    SettingsPatch.model_validate({"walk_in_ratio": {"walk_ins": 1, "appointments": 10}})
    for bad in (
        {"walk_ins": 1},
        {"walk_ins": 0, "appointments": 1},
        {"walk_ins": 1, "appointments": 11},
    ):
        with pytest.raises(ValidationError):
            SettingsPatch.model_validate({"walk_in_ratio": bad})


@pytest.mark.parametrize("body", [{}, {"unknown": 1}])
def test_settings_empty_or_unknown_is_rejected(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SettingsPatch.model_validate(body)


def test_business_update_rules() -> None:
    assert BusinessUpdate.model_validate({"address": None}).address is None  # clearable
    bad_bodies: list[dict[str, object]] = [
        {},
        {"name": None},
        {"category": "x"},
        {"settings": {}},
        {"timezone": "Mars/Base"},
    ]
    for bad in bad_bodies:
        with pytest.raises(ValidationError):
            BusinessUpdate.model_validate(bad)


def test_empty_address_is_rejected_and_only_null_clears_it() -> None:
    with pytest.raises(ValidationError):
        BusinessCreate.model_validate(business(address=""))
    with pytest.raises(ValidationError):
        BusinessCreate.model_validate(business(address="   "))
    with pytest.raises(ValidationError):
        BusinessUpdate.model_validate({"address": ""})
    assert BusinessUpdate.model_validate({"address": None}).address is None
