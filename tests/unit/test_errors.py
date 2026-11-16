from fastapi import FastAPI
from httpx import AsyncClient

from backend.api.errors import PROBLEM_JSON


async def test_validation_error_never_echoes_the_password(client: AsyncClient) -> None:
    secret = "my-very-secret-password-value"
    response = await client.post(
        "/api/v1/auth/login", json={"email": "not-an-email", "password": secret}
    )
    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM_JSON
    assert secret not in response.text
    body = response.json()
    assert body["status"] == 422
    assert body["errors"][0]["loc"] == ["body", "email"]


async def test_short_password_rejected_on_register(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "a@example.com", "password": "short", "organization_name": "Acme"},
    )
    assert response.status_code == 422
    assert "short" not in response.text.replace('"string_too_short"', "")


async def test_unknown_fields_are_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "a@example.com", "password": "x", "is_admin": True},
    )
    assert response.status_code == 422


async def test_not_found_uses_problem_details(client: AsyncClient) -> None:
    response = await client.get("/no-such-route")
    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json()["request_id"] == response.headers["x-request-id"]


async def test_unhandled_error_returns_generic_500(app: FastAPI, client: AsyncClient) -> None:
    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("database password is hunter2")

    response = await client.get("/boom")
    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM_JSON
    assert "hunter2" not in response.text
    assert response.json()["request_id"] == response.headers["x-request-id"]


async def test_protected_route_without_token_is_401(client: AsyncClient) -> None:
    response = await client.get("/api/v1/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["content-type"] == PROBLEM_JSON


async def test_non_ip_client_address_is_not_stored() -> None:
    from starlette.requests import Request

    from backend.api.auth import _client_ip

    def request_from(host: str) -> Request:
        return Request({"type": "http", "client": (host, 1), "headers": []})

    assert _client_ip(request_from("testclient")) is None
    assert _client_ip(request_from("203.0.113.7")) == "203.0.113.7"
    assert _client_ip(request_from("2001:db8::1")) == "2001:db8::1"
