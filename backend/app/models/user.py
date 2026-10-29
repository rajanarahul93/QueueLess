from datetime import datetime

from sqlalchemy import CheckConstraint, text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, NowTs, UuidPk


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('customer', 'staff', 'owner', 'admin')", name="role"),
        CheckConstraint("email IS NOT NULL OR phone IS NOT NULL", name="email_or_phone"),
    )

    id: Mapped[UuidPk]
    name: Mapped[str]
    email: Mapped[str | None] = mapped_column(CITEXT, unique=True)
    phone: Mapped[str | None] = mapped_column(unique=True)
    password_hash: Mapped[str | None]
    phone_verified_at: Mapped[datetime | None]
    role: Mapped[str]
    no_show_count: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[NowTs]
