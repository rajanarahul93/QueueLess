import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, SmallInteger, text
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint, Range
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, NowTs, UuidPk

ACTIVE_APPOINTMENT = "status IN ('booked', 'checked_in', 'in_service')"


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('booked', 'checked_in', 'in_service', 'completed', 'cancelled', "
            "'no_show', 'rescheduled')",
            name="status",
        ),
        CheckConstraint("priority BETWEEN 0 AND 2", name="priority_range"),
        # Half-open [start, end) only: empty or unbounded ranges never overlap and would bypass
        # no_overlap, and [) lets back-to-back slots coexist.
        CheckConstraint(
            "lower_inc(during) AND NOT upper_inc(during) AND NOT isempty(during) "
            "AND NOT lower_inf(during) AND NOT upper_inf(during)",
            name="range_valid",
        ),
        ExcludeConstraint(
            ("resource_id", "="),
            ("during", "&&"),
            name="no_overlap",
            using="gist",
            where=text(ACTIVE_APPOINTMENT),
        ),
        Index("ix_appointments_customer_id_status", "customer_id", "status"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="RESTRICT"))
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id", ondelete="RESTRICT"))
    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id", ondelete="RESTRICT"))
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    during: Mapped[Range[datetime]] = mapped_column(TSTZRANGE)
    status: Mapped[str]
    priority: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    rescheduled_from_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="RESTRICT")
    )
    created_at: Mapped[NowTs]
