import asyncio
import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from backend.llm.anthropic import TOOL_NAME, AnthropicChatModel
from backend.llm.client import BudgetExceededError, CallRecord, LLMClient
from backend.llm.fake import KeylessChatModel, ScriptedChatModel, key_terms
from backend.llm.types import Message, ModelError


class Answer(BaseModel):
    text: str


def _client(model: Any, records: list[CallRecord], **kwargs: Any) -> LLMClient:
    async def record(call: CallRecord) -> None:
        records.append(call)

    defaults: dict[str, Any] = {"token_budget": 10_000, "backoff_seconds": 0}
    return LLMClient(model, recorder=record, **(defaults | kwargs))


def _failing_then(answers: list[Exception | BaseModel]) -> ScriptedChatModel:
    async def respond(system: str, messages: list[Message], schema: type[BaseModel]) -> BaseModel:
        item = answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return ScriptedChatModel(respond)


async def _ask(client: LLMClient) -> Answer:
    return await client.complete(
        purpose="test", system="s", messages=[Message("user", "q")], schema=Answer
    )


async def test_retryable_errors_are_retried_and_every_attempt_recorded() -> None:
    records: list[CallRecord] = []
    model = _failing_then(
        [ModelError("unavailable", "x"), ModelError("rate_limited", "y"), Answer(text="ok")]
    )
    answer = await _ask(_client(model, records, max_attempts=3))
    assert answer.text == "ok"
    assert [r.status for r in records] == ["error", "error", "ok"]
    assert [r.error_category for r in records] == ["unavailable", "rate_limited", None]


async def test_non_retryable_errors_fail_at_once() -> None:
    records: list[CallRecord] = []
    model = _failing_then([ModelError("bad_request", "no"), Answer(text="never")])
    with pytest.raises(ModelError) as error:
        await _ask(_client(model, records, max_attempts=3))
    assert error.value.category == "bad_request"
    assert len(records) == 1


async def test_attempts_are_bounded() -> None:
    records: list[CallRecord] = []
    model = _failing_then([ModelError("unavailable", "x")] * 10)
    with pytest.raises(ModelError):
        await _ask(_client(model, records, max_attempts=3))
    assert len(records) == 3


async def test_a_slow_model_times_out() -> None:
    async def slow(system: str, messages: list[Message], schema: type[BaseModel]) -> BaseModel:
        await asyncio.sleep(5)
        return Answer(text="late")

    records: list[CallRecord] = []
    with pytest.raises(ModelError) as error:
        await _ask(_client(ScriptedChatModel(slow), records, max_attempts=2, timeout_seconds=0.05))
    assert error.value.category == "timeout"
    assert len(records) == 2


async def test_the_token_budget_stops_further_calls() -> None:
    records: list[CallRecord] = []
    model = _failing_then([Answer(text="a" * 400), Answer(text="b")])
    client = _client(model, records, token_budget=50)
    await _ask(client)  # allowed: nothing used yet
    assert client.tokens_used > 50
    with pytest.raises(BudgetExceededError):
        await _ask(client)
    assert len(records) == 1


async def test_cost_is_computed_only_when_prices_are_configured() -> None:
    unpriced: list[CallRecord] = []
    await _ask(_client(_failing_then([Answer(text="x")]), unpriced))
    priced: list[CallRecord] = []
    await _ask(
        _client(
            _failing_then([Answer(text="x")]),
            priced,
            input_price_per_mtok=3.0,
            output_price_per_mtok=15.0,
        )
    )
    assert unpriced[0].cost_usd is None
    record = priced[0]
    expected = (record.input_tokens * 3.0 + record.output_tokens * 15.0) / 1_000_000
    assert record.cost_usd == pytest.approx(expected)


async def test_keyless_model_plans_the_question_and_its_key_terms() -> None:
    from backend.orchestration.stages import QueryPlan

    result = await KeylessChatModel().complete(
        system="", messages=[Message("user", "What is the retention period for logs?")],
        schema=QueryPlan, max_tokens=100, timeout_seconds=1,
    )  # fmt: skip
    assert result.value.queries == [
        "What is the retention period for logs?",
        key_terms("What is the retention period for logs?"),
    ]


def _anthropic(handler: Any) -> AnthropicChatModel:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AnthropicChatModel("test-key", "claude-test", client)


async def _anthropic_ask(model: AnthropicChatModel) -> Answer:
    result = await model.complete(
        system="sys", messages=[Message("user", "hi")], schema=Answer,
        max_tokens=50, timeout_seconds=5,
    )  # fmt: skip
    return result.value


async def test_anthropic_request_forces_the_schema_tool_and_parses_the_answer() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "tool_use", "name": TOOL_NAME, "input": {"text": "hello"}}],
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
        )

    model = _anthropic(handler)
    result = await model.complete(
        system="sys", messages=[Message("user", "hi")], schema=Answer,
        max_tokens=50, timeout_seconds=5,
    )  # fmt: skip
    assert result.value.text == "hello"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 3)
    assert seen["headers"]["x-api-key"] == "test-key"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["body"]["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert seen["body"]["tools"][0]["input_schema"]["properties"]["text"]["type"] == "string"
    assert seen["body"]["system"] == "sys"


@pytest.mark.parametrize(
    ("status", "category"),
    [(401, "auth"), (429, "rate_limited"), (500, "unavailable"), (529, "unavailable"),
     (400, "bad_request")],
)  # fmt: skip
async def test_anthropic_http_errors_map_to_categories(status: int, category: str) -> None:
    model = _anthropic(lambda request: httpx.Response(status, json={"error": {}}))
    with pytest.raises(ModelError) as error:
        await _anthropic_ask(model)
    assert error.value.category == category


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "text", "text": "I refuse to use the tool"}],
        [{"type": "tool_use", "name": TOOL_NAME, "input": {"wrong": 1}}],
    ],
)
async def test_anthropic_malformed_answers_are_invalid_responses(content: list[Any]) -> None:
    model = _anthropic(lambda request: httpx.Response(200, json={"content": content, "usage": {}}))
    with pytest.raises(ModelError) as error:
        await _anthropic_ask(model)
    assert error.value.category == "invalid_response"


async def test_anthropic_connection_failures_are_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ModelError) as error:
        await _anthropic_ask(_anthropic(handler))
    assert error.value.category == "unavailable"


@pytest.mark.parametrize("key", [None, "", "   "])
def test_a_missing_or_empty_api_key_is_a_configuration_error(key: str | None) -> None:
    from backend.config import Settings
    from backend.llm.client import build_chat_model

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u@h/d",
        redis_url="redis://h",
        jwt_secret="x" * 32,
        llm_provider="anthropic",
        anthropic_api_key=key,
    )
    with pytest.raises(ModelError) as error:
        build_chat_model(settings)
    assert error.value.category == "configuration"
