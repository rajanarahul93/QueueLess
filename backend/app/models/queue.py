import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, NowTs, UuidPk

ACTIVE_TOKEN = "status IN ('waiting', 'called', 'in_service')"


class Queue(Base):
    __tablename__ = "queues"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'paused', 'closed')", name="status"),
        UniqueConstraint("resource_id", "queue_date", name="uq_queues_resource_date"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="RESTRICT"))
    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id", ondelete="RESTRICT"))
    queue_date: Mapped[date]
    last_token_no: Mapped[int] = mapped_column(server_default=text("0"))
    status: Mapped[str]


class QueueToken(Base):
    __tablename__ = "queue_tokens"
    __table_args__ = (
        CheckConstraint("source IN ('walk_in', 'appointment')", name="source"),
        CheckConstraint(
            "status IN ('waiting', 'called', 'in_service', 'completed', 'no_show', 'cancelled')",
            name="status",
        ),
        CheckConstraint("priority BETWEEN 0 AND 2", name="priority_range"),
        UniqueConstraint("queue_id", "token_no", name="uq_token"),
        UniqueConstraint("appointment_id", name="uq_queue_tokens_appointment_id"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="RESTRICT"))
    queue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("queues.id", ondelete="RESTRICT"))
    token_no: Mapped[int]
    source: Mapped[str]
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="RESTRICT")
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    status: Mapped[str]
    priority: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    skip_count: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    joined_at: Mapped[NowTs]
    called_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    queued_at: Mapped[NowTs]
    predicted_wait_sec: Mapped[int | None]
    version: Mapped[int] = mapped_column(server_default=text("1"))


Index(
    "one_active_token",
    QueueToken.queue_id,
    QueueToken.customer_id,
    unique=True,
    postgresql_where=text(ACTIVE_TOKEN),
)
Index(
    "active_tokens",
    QueueToken.queue_id,
    QueueToken.priority.desc(),
    QueueToken.queued_at,
    postgresql_where=text(ACTIVE_TOKEN),
)


class ServiceSession(Base):
    __tablename__ = "service_sessions"
    __table_args__ = (Index("ix_service_sessions_service_id_ended_at", "service_id", "ended_at"),)

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="RESTRICT"))
    token_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("queue_tokens.id", ondelete="RESTRICT"))
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id", ondelete="RESTRICT"))
    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id", ondelete="RESTRICT"))
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    duration_sec: Mapped[int | None]
