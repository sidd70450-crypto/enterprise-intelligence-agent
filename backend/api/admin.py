"""Administration endpoints. M1 has one: list the members of your organization."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import Membership, Role, User
from backend.database.session import get_session
from backend.security.dependencies import Principal, require_permission

router = APIRouter(prefix="/admin", tags=["admin"])


class Member(BaseModel):
    user_id: UUID
    email: str
    role: str
    is_active: bool
    joined_at: datetime


@router.get("/members", responses={403: {"description": "Role lacks members:read"}})
async def list_members(
    principal: Annotated[Principal, Depends(require_permission("members:read"))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[Member]:
    rows = await session.execute(
        select(User.id, User.email, Role.name, User.is_active, Membership.created_at)
        .select_from(Membership)
        .join(User, User.id == Membership.user_id)
        .join(Role, Role.id == Membership.role_id)
        # The organization comes from the verified token, never from the request.
        .where(Membership.org_id == principal.org_id)
        .order_by(Membership.created_at, User.id)
    )
    return [
        Member(user_id=uid, email=email, role=role, is_active=active, joined_at=joined)
        for uid, email, role, active, joined in rows
    ]
