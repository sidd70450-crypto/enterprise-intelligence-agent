from backend.security.passwords import hash_password, needs_rehash, verify_password


async def test_correct_password_verifies() -> None:
    stored = await hash_password("correct horse battery staple")
    assert stored.startswith("$argon2id$")
    assert await verify_password(stored, "correct horse battery staple")


async def test_wrong_password_fails() -> None:
    stored = await hash_password("correct horse battery staple")
    assert not await verify_password(stored, "correct horse battery stapler")


async def test_missing_user_never_verifies() -> None:
    # Even the dummy password must not pass when there is no stored hash.
    assert not await verify_password(None, "not-a-real-password-used-for-timing-only")


async def test_corrupt_hash_fails_instead_of_raising() -> None:
    assert not await verify_password("not-an-argon2-hash", "anything")


async def test_fresh_hash_does_not_need_rehash() -> None:
    assert not needs_rehash(await hash_password("correct horse battery staple"))
