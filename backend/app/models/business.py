import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UuidPk


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[UuidPk]
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    name: Mapped[str]
    category: Mapped[str]
    address: Mapped[str | None]
    timezone: Mapped[str]
    open_hours: Mapped[dict[str, Any]]
    settings: Mapped[dict[str, Any]]


class BusinessMember(Base):
    __tablename__ = "business_members"
    __table_args__ = (CheckConstraint("role IN ('owner', 'staff')", name="role"),)

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="RESTRICT"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    role: Mapped[str]


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (
        CheckConstraint("duration_min > 0", name="duration_min_positive"),
        CheckConstraint("buffer_min >= 0", name="buffer_min_non_negative"),
        Index("ix_services_business_id_is_active", "business_id", "is_active"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="RESTRICT"))
    name: Mapped[str]
    duration_min: Mapped[int]
    buffer_min: Mapped[int] = mapped_column(server_default=text("0"))
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))


class Resource(Base):
    __tablename__ = "resources"

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="RESTRICT"))
    name: Mapped[str]
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
