import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, NowTs, UuidPk


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    # Reuse detection revokes a whole family, so family lookups need an index.
    __table_args__ = (Index("ix_refresh_tokens_family_id", "family_id"),)

    id: Mapped[UuidPk]
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    family_id: Mapped[uuid.UUID]
    token_hash: Mapped[str] = mapped_column(unique=True)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[NowTs]


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_business_id_created_at", "business_id", "created_at"),)

    # business_id has no FK: audit rows outlive the entities they describe.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[uuid.UUID]
    # NULL means the system (a worker job); every request writes the acting user.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    action: Mapped[str]
    entity_type: Mapped[str]
    entity_id: Mapped[uuid.UUID | None]
    data: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[NowTs]
