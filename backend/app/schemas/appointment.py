import uuid

from pydantic import BaseModel


class SlotOut(BaseModel):
    """ISO 8601 with the business-local offset, for example 2026-11-02T10:30:00+05:30."""

    start: str
    end: str  # start + service duration; the buffer is not shown
    resource_id: uuid.UUID


class AvailabilityOut(BaseModel):
    timezone: str
    slots: list[SlotOut]
