import uuid
from datetime import date

from sqlalchemy import CheckConstraint, ForeignKey, SmallInteger, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DailyStat(Base):
    __tablename__ = "daily_stats"
    __table_args__ = (CheckConstraint("hour BETWEEN 0 AND 23", name="hour_range"),)

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="RESTRICT"), primary_key=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"), primary_key=True
    )
    stat_date: Mapped[date] = mapped_column(primary_key=True)
    hour: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    tokens_served: Mapped[int] = mapped_column(server_default=text("0"))
    avg_wait_sec: Mapped[int | None]
    p90_wait_sec: Mapped[int | None]
    no_shows: Mapped[int] = mapped_column(server_default=text("0"))
    tokens_joined: Mapped[int] = mapped_column(server_default=text("0"))
    cancelled: Mapped[int] = mapped_column(server_default=text("0"))
