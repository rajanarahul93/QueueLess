import uuid
from datetime import date

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Identity, Index, SmallInteger, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DailyStat(Base):
    """Nightly rollup (FR-31). service_id NULL: tokens that never started and have no
    appointment (walk-ins that left, were cancelled or did not show). hour NULL: the whole-day
    row of that service, which holds the day's exact average and p90 (hourly p90s cannot be
    combined)."""

    __tablename__ = "daily_stats"
    __table_args__ = (
        CheckConstraint("hour BETWEEN 0 AND 23", name="hour_range"),  # NULL passes
        Index(
            "uq_daily_stats_key",
            "business_id",
            "service_id",
            "stat_date",
            "hour",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="RESTRICT"))
    service_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT")
    )
    stat_date: Mapped[date]
    hour: Mapped[int | None] = mapped_column(SmallInteger)
    tokens_served: Mapped[int] = mapped_column(server_default=text("0"))
    avg_wait_sec: Mapped[int | None]
    p90_wait_sec: Mapped[int | None]
    no_shows: Mapped[int] = mapped_column(server_default=text("0"))
    tokens_joined: Mapped[int] = mapped_column(server_default=text("0"))
    cancelled: Mapped[int] = mapped_column(server_default=text("0"))
    avg_predicted_wait_sec: Mapped[int | None]
