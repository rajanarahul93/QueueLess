import uuid
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt


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


class JoinIn(BaseModel):
    """A walk-in join has no fields; anything sent is rejected."""

    model_config = ConfigDict(extra="forbid")


class QueueStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["open", "paused", "closed"]


class TokenOut(BaseModel):
    """The customer's own token. Times are ISO 8601 with the business-local offset. Priority is
    a staff matter and stays out of customer responses."""

    id: uuid.UUID
    queue_id: uuid.UUID
    token_no: int
    status: str
    source: str
    queued_at: str
    joined_at: str
    version: int
    # Waiting tokens ahead of this one in the base order; null once called or in service.
    # The estimated wait arrives later and is a separate calculation.
    ahead_count: int | None


class ActiveTokenOut(TokenOut):
    business_id: uuid.UUID
    queue_date: date
    queue_status: str


class VersionIn(BaseModel):
    """Optimistic lock: the version the staff member last saw."""

    model_config = ConfigDict(extra="forbid")

    version: StrictInt


class StartIn(VersionIn):
    # Required for walk-in tokens (they have no service); appointment tokens use the appointment's.
    service_id: uuid.UUID | None = None


class PriorityIn(VersionIn):
    priority: Annotated[StrictInt, Field(ge=0, le=2)]


class StreamTokenOut(BaseModel):
    stream_token: str
    expires_in: int
