import uuid
from datetime import date

from pydantic import BaseModel

# Aggregates only: no customer ids, names, phones or emails in any report.


class PeakHourOut(BaseModel):
    weekday: int  # Monday = 0, of the business-local date
    hour: int  # business-local hour of joining
    tokens: int


class WaitTimeOut(BaseModel):
    date: date
    service_id: uuid.UUID
    served: int
    avg_wait_sec: int
    p90_wait_sec: int | None  # null under 5 waits
    avg_predicted_wait_sec: int | None  # the ETA stored at join, over the same tokens


class NoShowOut(BaseModel):
    service_id: uuid.UUID | None  # null: walk-ins that never started
    joined: int
    no_shows: int
    cancelled: int
    no_show_rate: float
    cancel_rate: float
