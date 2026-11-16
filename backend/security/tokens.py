"""Access tokens (short-lived JWTs) and refresh tokens (opaque random strings).

Access tokens are HS256 JWTs carrying the user and organization IDs. They are
not stored; they expire. The role is deliberately *not* in the token: it is read
from the database on every request, so a role change or deactivation takes
effect immediately instead of when the token expires.

Refresh tokens are 256-bit random strings. The database stores only their
SHA-256 hash. A fast hash is enough here because the input is random, not a
human-chosen password, so it cannot be guessed from a stolen hash.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

ALGORITHM = "HS256"
ISSUER = "eia"
AUDIENCE = "eia-api"
TOKEN_TYPE = "access"  # noqa: S105  (a claim value, not a secret)
CLOCK_SKEW_SECONDS = 5


class InvalidTokenError(Exception):
    """The token is malformed, expired, forged, or not an access token."""


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    org_id: uuid.UUID


def create_access_token(
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    secret: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> str:
    issued = now or datetime.now(UTC)
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(user_id),
        "org": str(org_id),
        "typ": TOKEN_TYPE,
        "iat": issued,
        "exp": issued + timedelta(seconds=ttl_seconds),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_access_token(token: str, secret: str) -> AccessClaims:
    try:
        payload = jwt.decode(
            token,
            secret,
            # A fixed list: never trust the algorithm named in the token header.
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
        if payload.get("typ") != TOKEN_TYPE:
            raise InvalidTokenError("not an access token")
        return AccessClaims(user_id=uuid.UUID(payload["sub"]), org_id=uuid.UUID(payload["org"]))
    except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
        raise InvalidTokenError(type(exc).__name__) from exc


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
