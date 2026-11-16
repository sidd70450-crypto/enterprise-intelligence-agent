"""The current user."""

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from backend.security.dependencies import CurrentPrincipal

router = APIRouter(tags=["account"])


class MeResponse(BaseModel):
    user_id: UUID
    email: str
    organization_id: UUID
    organization_name: str
    role: str
    permissions: list[str]


@router.get("/me")
async def me(principal: CurrentPrincipal) -> MeResponse:
    return MeResponse(
        user_id=principal.user_id,
        email=principal.email,
        organization_id=principal.org_id,
        organization_name=principal.org_name,
        role=principal.role,
        permissions=sorted(principal.permissions),
    )
