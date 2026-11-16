import asyncio

from fastapi import FastAPI
from httpx import AsyncClient

from backend.api.health import Check, get_readiness_checks


async def _ok() -> None:
    return None


async def _boom() -> None:
    raise ConnectionError("password=hunter2 host=db.internal")


async def _hang() -> None:
    await asyncio.sleep(10)


def _use_checks(app: FastAPI, checks: dict[str, Check]) -> None:
    app.dependency_overrides[get_readiness_checks] = lambda: checks


async def test_healthz_is_ok_without_touching_dependencies(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_ready_when_all_checks_pass(app: FastAPI, client: AsyncClient) -> None:
    _use_checks(app, {"database": _ok, "redis": _ok})
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "redis": "ok"}}


async def test_readyz_503_names_the_failed_check(app: FastAPI, client: AsyncClient) -> None:
    _use_checks(app, {"database": _boom, "redis": _ok})
    response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "failed", "redis": "ok"},
    }


async def test_readyz_does_not_leak_error_details(app: FastAPI, client: AsyncClient) -> None:
    _use_checks(app, {"database": _boom})
    response = await client.get("/readyz")
    assert "hunter2" not in response.text
    assert "db.internal" not in response.text


async def test_readyz_times_out_a_hanging_dependency(app: FastAPI, client: AsyncClient) -> None:
    _use_checks(app, {"database": _ok, "redis": _hang})
    response = await asyncio.wait_for(client.get("/readyz"), timeout=2)
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "ok", "redis": "failed"}
