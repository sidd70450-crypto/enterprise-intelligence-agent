"""The one way the rest of the system calls a model.

``LLMClient`` wraps a ``ChatModel`` with:

- a per-attempt timeout and at most ``max_attempts`` attempts, with exponential
  backoff, retrying only errors whose category is retryable;
- a token budget per task: a call is refused once the task has used its budget,
  so a loop can never spend without limit;
- a record of every attempt (tokens, latency, cost, outcome) handed to a
  ``CallRecorder``, which the workflow stores in ``model_calls``.

Cost is computed only when prices are configured. No prices are built in:
provider prices change, and a guessed price would be an invented number.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import BaseModel

from backend.config import Settings
from backend.llm.anthropic import AnthropicChatModel
from backend.llm.fake import KeylessChatModel
from backend.llm.types import ChatModel, Completion, ErrorCategory, Message, ModelError


class BudgetExceededError(Exception):
    pass


@dataclass(frozen=True)
class CallRecord:
    purpose: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_ms: int
    status: str  # "ok" or "error"
    error_category: ErrorCategory | None


CallRecorder = Callable[[CallRecord], Awaitable[None]]


class LLMClient:
    def __init__(
        self,
        model: ChatModel,
        *,
        recorder: CallRecorder,
        token_budget: int,
        tokens_used: int = 0,
        max_attempts: int = 3,
        timeout_seconds: float = 60.0,
        backoff_seconds: float = 1.0,
        input_price_per_mtok: float | None = None,
        output_price_per_mtok: float | None = None,
    ) -> None:
        self.model = model
        self.tokens_used = tokens_used
        self._recorder = recorder
        self._budget = token_budget
        self._max_attempts = max_attempts
        self._timeout = timeout_seconds
        self._backoff = backoff_seconds
        self._prices = (input_price_per_mtok, output_price_per_mtok)

    def _cost(self, input_tokens: int, output_tokens: int) -> float | None:
        price_in, price_out = self._prices
        if price_in is None or price_out is None:
            return None
        return (input_tokens * price_in + output_tokens * price_out) / 1_000_000

    async def complete[T: BaseModel](
        self,
        *,
        purpose: str,
        system: str,
        messages: list[Message],
        schema: type[T],
        max_tokens: int = 1024,
    ) -> T:
        for attempt in range(1, self._max_attempts + 1):
            if self.tokens_used >= self._budget:
                raise BudgetExceededError(f"Token budget of {self._budget} used up.")
            started = time.perf_counter()
            try:
                async with asyncio.timeout(self._timeout):
                    result: Completion[T] = await self.model.complete(
                        system=system,
                        messages=messages,
                        schema=schema,
                        max_tokens=max_tokens,
                        timeout_seconds=self._timeout,
                    )
            except (ModelError, TimeoutError) as exc:
                error = exc if isinstance(exc, ModelError) else ModelError("timeout", "timed out")
                await self._record(purpose, started, 0, 0, error.category)
                if not error.retryable or attempt == self._max_attempts:
                    raise error from None
                await asyncio.sleep(self._backoff * 2 ** (attempt - 1))
                continue
            self.tokens_used += result.usage.total
            await self._record(
                purpose, started, result.usage.input_tokens, result.usage.output_tokens, None
            )
            return result.value
        raise AssertionError("unreachable")

    async def _record(
        self,
        purpose: str,
        started: float,
        input_tokens: int,
        output_tokens: int,
        error: ErrorCategory | None,
    ) -> None:
        await self._recorder(
            CallRecord(
                purpose=purpose,
                provider=self.model.provider,
                model=self.model.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=self._cost(input_tokens, output_tokens),
                latency_ms=round((time.perf_counter() - started) * 1000),
                status="error" if error else "ok",
                error_category=error,
            )
        )


def build_chat_model(settings: Settings) -> ChatModel:
    if settings.llm_provider == "fake":
        return KeylessChatModel()
    key = settings.anthropic_api_key
    if key is None or not key.get_secret_value().strip():  # unset, or "EIA_ANTHROPIC_API_KEY="
        raise ModelError("configuration", "EIA_ANTHROPIC_API_KEY is not set.")
    return AnthropicChatModel(key.get_secret_value(), settings.llm_model)
