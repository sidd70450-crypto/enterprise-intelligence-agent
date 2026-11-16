"""Anthropic Messages API adapter.

Structured output uses a forced tool call: the schema is offered as the only
tool, ``tool_choice`` requires it, and the tool's input is validated against the
Pydantic schema. HTTP failures are mapped to ``ModelError`` categories.

UNVERIFIED: this adapter has not been run against the real API, because no API
key was available while it was written. Its request format follows the public
Messages API documentation. Test it with a real key before relying on it.
"""

from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from backend.llm.types import Completion, Message, ModelError, Usage

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
TOOL_NAME = "respond"


class AnthropicChatModel:
    provider = "anthropic"

    def __init__(self, api_key: str, model: str, client: httpx.AsyncClient | None = None) -> None:
        self.model = model
        self._api_key = api_key
        self._client = client or httpx.AsyncClient()

    async def complete[T: BaseModel](
        self,
        *,
        system: str,
        messages: list[Message],
        schema: type[T],
        max_tokens: int,
        timeout_seconds: float,
    ) -> Completion[T]:
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "tools": [
                {
                    "name": TOOL_NAME,
                    "description": "Return the answer in exactly this structure.",
                    "input_schema": schema.model_json_schema(),
                }
            ],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
        }
        headers = {"x-api-key": self._api_key, "anthropic-version": API_VERSION}
        try:
            response = await self._client.post(
                API_URL, json=body, headers=headers, timeout=timeout_seconds
            )
        except httpx.TimeoutException as exc:
            raise ModelError("timeout", "The model did not answer in time.") from exc
        except httpx.TransportError as exc:
            raise ModelError("unavailable", type(exc).__name__) from exc

        _raise_for_status(response)
        payload: dict[str, Any] = response.json()
        usage = payload.get("usage", {})
        tool_input = next(
            (
                block.get("input")
                for block in payload.get("content", [])
                if block.get("type") == "tool_use" and block.get("name") == TOOL_NAME
            ),
            None,
        )
        if tool_input is None:
            raise ModelError("invalid_response", "The model did not return structured output.")
        try:
            value = schema.model_validate(tool_input)
        except ValidationError as exc:
            raise ModelError("invalid_response", "The output did not match the schema.") from exc
        return Completion(
            value, Usage(int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)))
        )


def _raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if status < 400:
        return
    if status in (401, 403):
        raise ModelError("auth", "The API key was rejected.")
    if status == 429:
        raise ModelError("rate_limited", "Rate limited by the provider.")
    if status >= 500 or status == 529:
        raise ModelError("unavailable", f"Provider returned HTTP {status}.")
    raise ModelError("bad_request", f"Provider rejected the request with HTTP {status}.")
