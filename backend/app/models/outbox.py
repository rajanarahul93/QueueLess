import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, NowTs


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_unpublished", "id", postgresql_where=text("published_at IS NULL")),
        Index("ix_outbox_queue_id_id", "queue_id", "id"),
    )

    # The id is the SSE event id. business_id and queue_id carry no FK: append-only log that is
    # cleaned by age, and booking events have no queue.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[uuid.UUID]
    queue_id: Mapped[uuid.UUID | None]
    event_type: Mapped[str]
    payload: Mapped[dict[str, Any]]
    created_at: Mapped[NowTs]
    published_at: Mapped[datetime | None]
