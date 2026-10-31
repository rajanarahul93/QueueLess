import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import SessionDep, get_clock, require_business_member
from app.core.errors import ValidationFailed
from app.core.rate_limit import user_rate_limit
from app.core.timezones import get_zone
from app.schemas.report import NoShowOut, PeakHourOut, WaitTimeOut
from app.services import reports as report_service
from app.services.business import get_business

# Owner only. Business ids are public, so another business's owner gets 403 like any other
# non-owner, and only an unknown business id is 404 (as every owner endpoint).
router = APIRouter(
    prefix="/businesses/{business_id}/reports",
    tags=["reports"],
    dependencies=[
        Depends(require_business_member("owner")),
        Depends(user_rate_limit("reports_user", limit=30, window_sec=60)),
    ],
)

ClockDep = Annotated[Callable[[], float], Depends(get_clock)]
From = Annotated[date | None, Query(alias="from")]
To = Annotated[date | None, Query(alias="to")]
DEFAULT_DAYS = 30
MAX_DAYS = 92


async def _rows(
    session: SessionDep,
    business_id: uuid.UUID,
    clock: ClockDep,
    first: From = None,
    last: To = None,
) -> list[report_service.Stat]:
    """Business-local dates, both inclusive; default the last 30 days through today."""
    business = await get_business(session, business_id)
    today = datetime.fromtimestamp(clock(), UTC).astimezone(get_zone(business.timezone)).date()
    last = last or today
    first = first or last - timedelta(days=DEFAULT_DAYS - 1)
    if last > today:
        raise ValidationFailed(["query", "to"], "Cannot be after today")
    if first > last:
        raise ValidationFailed(["query", "from"], "Must not be after 'to'")
    if (last - first).days + 1 > MAX_DAYS:
        raise ValidationFailed(["query", "from"], f"The range is at most {MAX_DAYS} days")
    return await report_service.stat_rows(
        session, business_id, business.timezone, first, last, today
    )


Rows = Annotated[list[report_service.Stat], Depends(_rows)]


@router.get("/peak-hours")
async def peak_hours(rows: Rows) -> list[PeakHourOut]:
    """Tokens joined by weekday and business-local hour."""
    return report_service.peak_hours(rows)


@router.get("/wait-times")
async def wait_times(rows: Rows, service_id: uuid.UUID | None = None) -> list[WaitTimeOut]:
    """Per day and service: served tokens, average and p90 wait, average predicted wait."""
    return report_service.wait_times(rows, service_id)


@router.get("/no-shows")
async def no_shows(rows: Rows) -> list[NoShowOut]:
    """Per service over the range: joined, no-shows, cancelled, and their rates."""
    return report_service.no_shows(rows)
