"""Password hashing with Argon2id.

Hashing is deliberately slow (tens of milliseconds of CPU), so it runs in a
worker thread to keep the event loop free for other requests.

When the user does not exist, ``verify_password`` still runs a full Argon2
verification against a fixed dummy hash, so a login for an unknown email takes
as long as a login with a wrong password and response time does not reveal which
accounts exist.
"""

import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("not-a-real-password-used-for-timing-only")


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password_hash: str | None, password: str) -> bool:
    """True only if ``password_hash`` is a real hash and the password matches it."""

    def _verify() -> bool:
        try:
            _hasher.verify(password_hash or _DUMMY_HASH, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        return password_hash is not None

    return await asyncio.to_thread(_verify)


def needs_rehash(password_hash: str) -> bool:
    """True when the hash was made with weaker parameters than the current ones."""
    return _hasher.check_needs_rehash(password_hash)
