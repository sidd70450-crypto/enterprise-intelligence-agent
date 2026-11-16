import re

from httpx import AsyncClient


async def test_response_carries_a_generated_request_id(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


async def test_each_request_gets_its_own_id(client: AsyncClient) -> None:
    first = await client.get("/healthz")
    second = await client.get("/healthz")
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


async def test_a_safe_incoming_id_is_kept(client: AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "trace-123.abc"})
    assert response.headers["x-request-id"] == "trace-123.abc"


async def test_an_unsafe_incoming_id_is_replaced(client: AsyncClient) -> None:
    hostile = 'x" injected="1'
    response = await client.get("/healthz", headers={"X-Request-ID": hostile})
    assert response.headers["x-request-id"] != hostile
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


async def test_an_overlong_incoming_id_is_replaced(client: AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "a" * 65})
    assert len(response.headers["x-request-id"]) == 32
