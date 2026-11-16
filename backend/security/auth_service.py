"""Registration, login, token refresh and logout.

Routes stay thin: they translate HTTP into calls on ``AuthService`` and its
exceptions back into HTTP. Every method commits its own transaction, including
the audit record of what it did.
"""

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.database.models import Membership, Organization, RefreshToken, Role, User
from backend.security.audit import record_event
from backend.security.passwords import hash_password, needs_rehash, verify_password
from backend.security.rate_limit import LoginRateLimiter
from backend.security.tokens import create_access_token, hash_refresh_token, new_refresh_token

OWNER_ROLE = "owner"


class EmailAlreadyRegisteredError(Exception):
    pass


class InvalidCredentialsError(Exception):
    """Wrong email or password, inactive user, or no membership. Deliberately vague."""


class TooManyAttemptsError(Exception):
    pass


class InvalidRefreshTokenError(Exception):
    pass


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True)
class RegisteredAccount:
    user_id: uuid.UUID
    email: str
    org_id: uuid.UUID
    org_name: str
    role: str


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:100] or "org"
    return f"{base}-{secrets.token_hex(3)}"


class AuthService:
    def __init__(
        self, session: AsyncSession, settings: Settings, limiter: LoginRateLimiter
    ) -> None:
        self._session = session
        self._settings = settings
        self._limiter = limiter

    async def register(
        self, email: str, password: str, org_name: str, ip: str | None
    ) -> RegisteredAccount:
        """Create a user, a new organization, and make the user its owner."""
        owner = await self._session.scalar(select(Role).where(Role.name == OWNER_ROLE))
        if owner is None:
            raise RuntimeError("Built-in roles are missing; run the database migrations.")

        user = User(id=uuid.uuid4(), email=email, password_hash=await hash_password(password))
        org = Organization(id=uuid.uuid4(), name=org_name, slug=_slugify(org_name))
        self._session.add_all([user, org])
        try:
            # The flush inserts the user, which is where a duplicate email fails.
            await self._session.flush()
            self._session.add(Membership(user_id=user.id, org_id=org.id, role_id=owner.id))
            record_event(
                self._session,
                "auth.registered",
                org_id=org.id,
                actor_user_id=user.id,
                ip=ip,
                resource_type="user",
                resource_id=str(user.id),
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if "uq_users_email" in str(exc.orig):
                raise EmailAlreadyRegisteredError from exc
            raise
        return RegisteredAccount(user.id, user.email, org.id, org.name, OWNER_ROLE)

    async def login(self, email: str, password: str, ip: str | None) -> IssuedTokens:
        if await self._limiter.is_blocked(email, ip):
            record_event(self._session, "auth.login_blocked", ip=ip)
            await self._session.commit()
            raise TooManyAttemptsError

        user = await self._session.scalar(select(User).where(User.email == email))
        password_ok = await verify_password(user.password_hash if user else None, password)
        membership = None
        if user is not None and password_ok and user.is_active:
            # One organization per login in v0.1: the user's oldest membership.
            membership = await self._session.scalar(
                select(Membership)
                .where(Membership.user_id == user.id)
                .order_by(Membership.created_at, Membership.id)
                .limit(1)
            )

        if user is None or membership is None:
            await self._limiter.record_failure(email, ip)
            record_event(
                self._session,
                "auth.login_failed",
                actor_user_id=user.id if user else None,
                ip=ip,
            )
            await self._session.commit()
            raise InvalidCredentialsError

        if needs_rehash(user.password_hash):
            user.password_hash = await hash_password(password)
        await self._limiter.clear(email)
        tokens, _ = self._issue(user.id, membership.org_id, family_id=uuid.uuid4())
        record_event(
            self._session,
            "auth.login_succeeded",
            org_id=membership.org_id,
            actor_user_id=user.id,
            ip=ip,
        )
        await self._session.commit()
        return tokens

    async def refresh(self, presented: str, ip: str | None) -> IssuedTokens:
        """Exchange a refresh token for a new access token and a new refresh token."""
        now = datetime.now(UTC)
        # FOR UPDATE serialises concurrent refreshes of the same token: the second
        # request waits, then sees the token already revoked.
        token = await self._session.scalar(
            select(RefreshToken)
            .where(RefreshToken.token_hash == hash_refresh_token(presented))
            .with_for_update()
        )
        if token is None:
            raise InvalidRefreshTokenError

        if token.revoked_at is not None and token.replaced_by_id is None:
            # Revoked by logout or by an earlier family revocation: simply invalid.
            raise InvalidRefreshTokenError

        if token.revoked_at is not None:
            # A token that was already rotated came back: it was stolen or replayed.
            # Kill the family, which also logs out whoever holds the current token.
            await self._revoke_family(token.family_id, now)
            record_event(
                self._session,
                "auth.refresh_reuse_detected",
                org_id=token.org_id,
                actor_user_id=token.user_id,
                ip=ip,
                details={"family_id": str(token.family_id)},
            )
            await self._session.commit()
            raise InvalidRefreshTokenError

        if token.expires_at <= now or not await self._membership_active(token):
            await self._revoke_family(token.family_id, now)
            await self._session.commit()
            raise InvalidRefreshTokenError

        tokens, successor_id = self._issue(token.user_id, token.org_id, family_id=token.family_id)
        await self._session.flush()  # the successor must exist before it is referenced
        token.revoked_at = now
        token.replaced_by_id = successor_id
        await self._session.commit()
        return tokens

    async def logout(self, presented: str | None, ip: str | None) -> None:
        """Revoke every token descended from the same login. Idempotent."""
        if not presented:
            return
        token = await self._session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(presented))
        )
        if token is None:
            return
        await self._revoke_family(token.family_id, datetime.now(UTC))
        record_event(
            self._session,
            "auth.logout",
            org_id=token.org_id,
            actor_user_id=token.user_id,
            ip=ip,
        )
        await self._session.commit()

    def _issue(
        self, user_id: uuid.UUID, org_id: uuid.UUID, family_id: uuid.UUID
    ) -> tuple[IssuedTokens, uuid.UUID]:
        settings = self._settings
        refresh = new_refresh_token()
        token_id = uuid.uuid4()
        self._session.add(
            RefreshToken(
                id=token_id,
                user_id=user_id,
                org_id=org_id,
                family_id=family_id,
                token_hash=hash_refresh_token(refresh),
                expires_at=datetime.now(UTC)
                + timedelta(seconds=settings.refresh_token_ttl_seconds),
            )
        )
        access = create_access_token(
            user_id,
            org_id,
            settings.jwt_secret.get_secret_value(),
            settings.access_token_ttl_seconds,
        )
        return IssuedTokens(access, refresh, settings.access_token_ttl_seconds), token_id

    async def _revoke_family(self, family_id: uuid.UUID, now: datetime) -> None:
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    async def _membership_active(self, token: RefreshToken) -> bool:
        found = await self._session.scalar(
            select(Membership.id)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.user_id == token.user_id,
                Membership.org_id == token.org_id,
                User.is_active.is_(True),
            )
        )
        return found is not None
