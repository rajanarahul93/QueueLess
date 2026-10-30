import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


def record_audit(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    data: dict[str, Any] | None = None,
) -> None:
    """Adds the row to the caller's transaction (FR-28); it never commits, so the audit row and
    the action it describes succeed or fail together."""
    session.add(
        AuditLog(
            business_id=business_id,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            data=data or {},
        )
    )
