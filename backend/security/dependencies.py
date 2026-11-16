"""FastAPI dependencies for authentication and authorization.

``get_principal`` turns a bearer token into a ``Principal``: who is calling,
in which organization, with which permissions. It checks the database on every
request, so deactivating a user or removing a membership takes effect at once.

``require_permission("members:read")`` wraps it and returns 403 when the role
lacks the permission.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.database.models import Membership, Organization, Role, User
from backend.database.session import get_session
from backend.security.tokens import InvalidTokenError, decode_access_token

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    email: str
    org_id: uuid.UUID
    org_name: str
    role: str
    permissions: frozenset[str]


def get_settings_from_app(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing, invalid or expired access token.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> Principal:
    if credentials is None:
        raise _unauthorized()
    try:
        claims = decode_access_token(
            credentials.credentials, settings.jwt_secret.get_secret_value()
        )
    except InvalidTokenError:
        raise _unauthorized() from None

    row = (
        await session.execute(
            select(User.email, Organization.name, Role.name, Role.permissions)
            .select_from(Membership)
            .join(User, User.id == Membership.user_id)
            .join(Organization, Organization.id == Membership.org_id)
            .join(Role, Role.id == Membership.role_id)
            .where(
                Membership.user_id == claims.user_id,
                Membership.org_id == claims.org_id,
                User.is_active.is_(True),
            )
        )
    ).one_or_none()
    if row is None:
        raise _unauthorized()

    email, org_name, role, permissions = row
    structlog.contextvars.bind_contextvars(user_id=str(claims.user_id), org_id=str(claims.org_id))
    return Principal(
        user_id=claims.user_id,
        email=email,
        org_id=claims.org_id,
        org_name=org_name,
        role=role,
        permissions=frozenset(permissions),
    )


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def require_permission(permission: str) -> Callable[..., Awaitable[Principal]]:
    async def dependency(principal: CurrentPrincipal) -> Principal:
        if permission not in principal.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your role does not allow this action.",
            )
        return principal

    return dependency
