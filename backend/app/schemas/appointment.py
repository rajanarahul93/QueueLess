import uuid

from pydantic import AwareDatetime, BaseModel, ConfigDict


class SlotOut(BaseModel):
    """ISO 8601 with the business-local offset, for example 2026-11-02T10:30:00+05:30."""

    start: str
    end: str  # start + service duration; the buffer is not shown
    resource_id: uuid.UUID


class AvailabilityOut(BaseModel):
    timezone: str
    slots: list[SlotOut]


class AppointmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: uuid.UUID
    resource_id: uuid.UUID
    start: AwareDatetime  # a naive timestamp is a 422


class RescheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: AwareDatetime  # service and resource cannot change on reschedule in v1


class CustomerOut(BaseModel):
    id: uuid.UUID
    name: str


class AppointmentOut(BaseModel):
    """Times are ISO 8601 in the business-local offset."""

    id: uuid.UUID
    business_id: uuid.UUID
    service_id: uuid.UUID
    service_name: str
    resource_id: uuid.UUID
    start: str
    end: str  # start + the service's current duration_min; the stored buffer is not shown
    status: str
    created_at: str


class RescheduledOut(AppointmentOut):
    rescheduled_from_id: uuid.UUID


class StaffAppointmentOut(AppointmentOut):
    customer: CustomerOut


class AppointmentPage(BaseModel):
    items: list[AppointmentOut]
    next_cursor: str | None


class StaffAppointmentPage(BaseModel):
    items: list[StaffAppointmentOut]
    next_cursor: str | None
