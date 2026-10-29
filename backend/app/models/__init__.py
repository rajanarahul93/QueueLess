"""Importing this package registers every table on Base.metadata (Alembic and tests rely on it)."""

from app.db.base import Base
from app.models.appointment import Appointment
from app.models.auth import AuditLog, RefreshToken
from app.models.business import Business, BusinessMember, Resource, Service
from app.models.outbox import OutboxEvent
from app.models.queue import Queue, QueueToken, ServiceSession
from app.models.stats import DailyStat
from app.models.user import User

__all__ = [
    "Appointment",
    "AuditLog",
    "Base",
    "Business",
    "BusinessMember",
    "DailyStat",
    "OutboxEvent",
    "Queue",
    "QueueToken",
    "RefreshToken",
    "Resource",
    "Service",
    "ServiceSession",
    "User",
]
