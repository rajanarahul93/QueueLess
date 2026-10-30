import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict


class QueueOut(BaseModel):
    """Open to every authenticated user, so it never carries tokens or customer data."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    business_id: uuid.UUID
    resource_id: uuid.UUID
    queue_date: date  # the business-local day, YYYY-MM-DD
    status: str


class SnapshotCustomer(BaseModel):
    id: uuid.UUID
    name: str  # never phone, email or password hash


class SnapshotToken(BaseModel):
    """Times are ISO 8601 with the business-local offset, or null."""

    id: uuid.UUID
    token_no: int
    status: str
    priority: int
    source: str
    skip_count: int
    version: int
    joined_at: str
    queued_at: str
    called_at: str | None
    started_at: str | None
    # 1-based place among waiting tokens in the base order (priority, then queued_at); null for
    # called and in_service. Call-next may apply the walk-in ratio, so it is not a promise.
    order_position: int | None
    appointment_id: uuid.UUID | None
    customer: SnapshotCustomer


class SnapshotCounts(BaseModel):
    completed: int
    no_show: int
    cancelled: int


class SnapshotOut(QueueOut):
    counts: SnapshotCounts
    tokens: list[SnapshotToken]  # active tokens only, in the base order
