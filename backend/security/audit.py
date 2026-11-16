"""Writing audit events.

``record_event`` only adds the row to the session; it is committed together
with the change it describes, so an action and its audit record either both
happen or neither does.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import AuditEvent


def record_event(
    session: AsyncSession,
    action: str,
    *,
    org_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    ip: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            org_id=org_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip=ip,
            details=details or {},
        )
    )
