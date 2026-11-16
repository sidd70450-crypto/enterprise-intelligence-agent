"""Authentication endpoints: register, login, refresh, logout.

The access token is returned in the response body and sent back by the client
in the ``Authorization`` header. The refresh token lives only in an httpOnly,
Secure, SameSite=Strict cookie scoped to ``/api/v1/auth``, so page scripts
cannot read it and the browser sends it nowhere except these endpoints.
"""

import ipaddress
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.errors import problem_response
from backend.config import Settings
from backend.database.session import get_session
from backend.security.auth_service import (
    AuthService,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    IssuedTokens,
    TooManyAttemptsError,
)
from backend.security.dependencies import get_settings_from_app
from backend.security.rate_limit import LoginRateLimiter, allow_attempt

REFRESH_COOKIE = "eia_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"

router = APIRouter(prefix="/auth", tags=["auth"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RegisterRequest(_Strict):
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=12, max_length=128, repr=False)
    organization_name: str = Field(min_length=1, max_length=100)

    @field_validator("email")
    @classmethod
    def _lowercase(cls, value: str) -> str:
        return value.lower()


class LoginRequest(_Strict):
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=1, max_length=128, repr=False)

    @field_validator("email")
    @classmethod
    def _lowercase(cls, value: str) -> str:
        return value.lower()


class RegisterResponse(BaseModel):
    user_id: UUID
    email: str
    organization_id: UUID
    organization_name: str
    role: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  (a scheme name, not a secret)
    expires_in: int


def get_auth_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> AuthService:
    limiter = LoginRateLimiter(
        request.app.state.redis, settings.login_max_failures, settings.login_window_seconds
    )
    return AuthService(session, settings, limiter)


Service = Annotated[AuthService, Depends(get_auth_service)]


def _client_ip(request: Request) -> str | None:
    """The peer's IP address, or None if the server did not report a real one.

    This is the direct peer. Behind a reverse proxy it must come from the proxy's
    forwarded header with trusted hosts configured; that is set up in M7.
    """
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


def _set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=settings.refresh_token_ttl_seconds,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="strict",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, httponly=True, secure=True, samesite="strict"
    )


def _token_response(tokens: IssuedTokens, settings: Settings) -> JSONResponse:
    body = TokenResponse(access_token=tokens.access_token, expires_in=tokens.expires_in)
    response = JSONResponse(body.model_dump(), headers={"Cache-Control": "no-store"})
    _set_refresh_cookie(response, tokens.refresh_token, settings)
    return response


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "Email already registered"},
        429: {"description": "Too many registrations from this address"},
    },
)
async def register(
    body: RegisterRequest,
    request: Request,
    service: Service,
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> RegisterResponse:
    ip = _client_ip(request)
    if ip and not await allow_attempt(
        request.app.state.redis,
        f"rl:register:ip:{ip}",
        settings.registrations_per_ip_per_hour,
        3600,
    ):
        raise HTTPException(429, "Too many registrations from this address. Try again later.")
    try:
        account = await service.register(body.email, body.password, body.organization_name, ip)
    except EmailAlreadyRegisteredError:
        raise HTTPException(409, "An account with this email already exists.") from None
    return RegisterResponse(
        user_id=account.user_id,
        email=account.email,
        organization_id=account.org_id,
        organization_name=account.org_name,
        role=account.role,
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    responses={
        401: {"description": "Invalid credentials"},
        429: {"description": "Too many attempts"},
    },
)
async def login(
    body: LoginRequest,
    request: Request,
    service: Service,
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> JSONResponse:
    try:
        tokens = await service.login(body.email, body.password, _client_ip(request))
    except InvalidCredentialsError:
        return problem_response(401, "Incorrect email or password.")
    except TooManyAttemptsError:
        return problem_response(
            429,
            "Too many failed attempts. Try again later.",
            headers={"Retry-After": str(settings.login_window_seconds)},
        )
    return _token_response(tokens, settings)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    responses={401: {"description": "Missing, invalid, expired or reused refresh token"}},
)
async def refresh(
    request: Request,
    service: Service,
    settings: Annotated[Settings, Depends(get_settings_from_app)],
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE, max_length=128)] = None,
) -> JSONResponse:
    if refresh_token is None:
        return problem_response(401, "No refresh token.")
    try:
        tokens = await service.refresh(refresh_token, _client_ip(request))
    except InvalidRefreshTokenError:
        response = problem_response(401, "Session expired. Log in again.")
        _clear_refresh_cookie(response)
        return response
    return _token_response(tokens, settings)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    service: Service,
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE, max_length=128)] = None,
) -> Response:
    await service.logout(refresh_token, _client_ip(request))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(response)
    return response
