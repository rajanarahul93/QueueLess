import base64
import binascii
import json
import uuid
import zoneinfo
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    model_validator,
)

from app.core.errors import ValidationFailed
from app.schemas.auth import MAX_PASSWORD_LENGTH, Email, Name

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
MAX_MINUTES = 1440


def _known_timezone(value: str) -> str:
    # Membership in the IANA list, not ZoneInfo(value): that also accepts "localtime" etc.
    if value not in zoneinfo.available_timezones():
        raise ValueError("unknown IANA timezone")
    return value


Address = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Timezone = Annotated[str, StringConstraints(max_length=64), AfterValidator(_known_timezone)]


class OpenInterval(BaseModel):
    """Local wall-clock time in the business timezone; half-open [open, close)."""

    model_config = ConfigDict(extra="forbid")

    open: HHMM
    close: HHMM

    @model_validator(mode="after")
    def open_before_close(self) -> Self:
        if self.open >= self.close:  # zero-padded HH:MM compares correctly as text
            raise ValueError("open must be before close")
        return self


OpenHours = dict[Weekday, Annotated[list[OpenInterval], Field(max_length=4)]]


class BusinessCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    category: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    address: Address | None = None
    timezone: Timezone
    open_hours: OpenHours


class BusinessOut(BaseModel):
    """Public view; settings stay private to the owner."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    category: str
    address: str | None
    timezone: str
    open_hours: dict[str, list[OpenInterval]]


class BusinessOwnerOut(BusinessOut):
    settings: dict[str, Any]


class MyBusinessOut(BusinessOut):
    role: str


class BusinessPage(BaseModel):
    items: list[BusinessOut]
    next_cursor: str | None


Minutes = Annotated[int, Field(ge=0, le=MAX_MINUTES)]
Price = Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=2)]


class ServiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    duration_min: Annotated[int, Field(gt=0, le=MAX_MINUTES)]
    buffer_min: Minutes = 0
    price: Price
    is_active: bool = True


class ServiceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    duration_min: Annotated[int, Field(gt=0, le=MAX_MINUTES)] | None = None
    buffer_min: Minutes | None = None
    price: Price | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def no_nulls(self) -> Self:
        # Omitted means unchanged; an explicit null would hit a NOT NULL column.
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("fields cannot be null")
        return self


class ServiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    duration_min: int
    buffer_min: int
    price: Decimal
    is_active: bool


def encode_cursor(name: str, business_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(json.dumps([name, str(business_id)]).encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, uuid.UUID]:
    """Strict: the cursor is only a position and carries no authority."""
    try:
        raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
        value = json.loads(raw)
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not all(isinstance(part, str) for part in value)
        ):
            raise ValueError("cursor must be [name, id]")
        return value[0], uuid.UUID(value[1])
    except (binascii.Error, ValueError) as error:  # JSONDecodeError and UnicodeError included
        raise ValidationFailed(["query", "cursor"], "Invalid cursor") from error


class WalkInRatio(BaseModel):
    """Replaced as a whole, so both keys are always sent together."""

    model_config = ConfigDict(extra="forbid")

    walk_ins: Annotated[StrictInt, Field(ge=1, le=10)]
    appointments: Annotated[StrictInt, Field(ge=1, le=10)]


class SettingsPatch(BaseModel):
    """Bounds per Architecture section 5; omitted keys stay unchanged, null is rejected."""

    model_config = ConfigDict(extra="forbid")

    grace_period_min: Annotated[StrictInt, Field(ge=0, le=60)] | None = None
    reschedule_cutoff_min: Annotated[StrictInt, Field(ge=0, le=10080)] | None = None
    promotion_wait_limit_min: Annotated[StrictInt, Field(ge=1, le=240)] | None = None
    running_late_threshold_min: Annotated[StrictInt, Field(ge=1, le=120)] | None = None
    walk_in_ratio: WalkInRatio | None = None
    max_walk_ins: Annotated[StrictInt, Field(ge=0, le=1000)] | None = None

    @model_validator(mode="after")
    def non_empty_no_nulls(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("settings must contain at least one key")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("settings values cannot be null")
        return self


class BusinessUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    address: Address | None = None
    timezone: Timezone | None = None
    open_hours: OpenHours | None = None
    settings: SettingsPatch | None = None

    @model_validator(mode="after")
    def at_least_one_no_nulls(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        # address may be cleared with an explicit null; everything else is NOT NULL.
        if any(getattr(self, name) is None for name in self.model_fields_set if name != "address"):
            raise ValueError("fields cannot be null")
        return self


class StaffCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    email: Email
    initial_password: Annotated[
        str, StringConstraints(min_length=10, max_length=MAX_PASSWORD_LENGTH)
    ]


class StaffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str | None


class StaffCreated(StaffOut):
    role: str
