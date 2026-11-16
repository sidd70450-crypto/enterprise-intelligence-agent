import base64
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from backend.security.tokens import (
    AUDIENCE,
    ISSUER,
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_refresh_token,
    new_refresh_token,
)

SECRET = secrets.token_urlsafe(48)
USER = uuid.uuid4()
ORG = uuid.uuid4()


def _b64(data: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def test_round_trip() -> None:
    claims = decode_access_token(create_access_token(USER, ORG, SECRET, 60), SECRET)
    assert (claims.user_id, claims.org_id) == (USER, ORG)


def test_expired_token_is_rejected() -> None:
    issued = datetime.now(UTC) - timedelta(minutes=10)
    token = create_access_token(USER, ORG, SECRET, 60, now=issued)
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, SECRET)


def test_token_signed_with_another_secret_is_rejected() -> None:
    token = create_access_token(USER, ORG, secrets.token_urlsafe(48), 60)
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, SECRET)


def test_unsigned_alg_none_token_is_rejected() -> None:
    now = int(datetime.now(UTC).timestamp())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(USER),
        "org": str(ORG),
        "typ": "access",
        "iat": now,
        "exp": now + 60,
    }
    token = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(payload)}."
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, SECRET)


def test_wrong_audience_is_rejected() -> None:
    now = datetime.now(UTC)
    token = jwt.encode(
        {"iss": ISSUER, "aud": "someone-else", "sub": str(USER), "org": str(ORG), "typ": "access",
         "iat": now, "exp": now + timedelta(minutes=1)},
        SECRET,
        algorithm="HS256",
    )  # fmt: skip
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, SECRET)


def test_token_of_another_type_is_rejected() -> None:
    now = datetime.now(UTC)
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": str(USER), "org": str(ORG), "typ": "refresh",
         "iat": now, "exp": now + timedelta(minutes=1)},
        SECRET,
        algorithm="HS256",
    )  # fmt: skip
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, SECRET)


@pytest.mark.parametrize("bad", ["", "not.a.jwt", "a.b.c"])
def test_garbage_is_rejected(bad: str) -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token(bad, SECRET)


def test_refresh_tokens_are_random_and_hashed() -> None:
    first, second = new_refresh_token(), new_refresh_token()
    assert first != second
    assert len(first) >= 43  # 32 random bytes, base64url
    assert hash_refresh_token(first) == hash_refresh_token(first)
    assert hash_refresh_token(first) != first
    assert len(hash_refresh_token(first)) == 64
