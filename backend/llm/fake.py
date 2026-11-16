"""A scripted model for tests and for running the system without an API key.

``ScriptedChatModel`` answers each request by calling a function you supply, so
tests control exactly what the "model" says, including failures.

``KeylessChatModel`` is what ``llm_provider = "fake"`` gives you: it produces
mechanical answers (for query planning, the question itself plus its key terms)
so the whole pipeline can be exercised end to end. It does no reasoning; results
from it say nothing about the quality of a real model.
"""

import re
from collections.abc import Awaitable, Callable
from typing import Any, cast

from pydantic import BaseModel

from backend.llm.types import Completion, Message, ModelError, Usage

Responder = Callable[[str, list[Message], type[BaseModel]], Awaitable[BaseModel]]

_STOP_WORDS = frozenset(
    "a an and are as at be by did do does for from how in is it of on or the to was "
    "were what when where which who why will with".split()
)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class ScriptedChatModel:
    provider = "scripted"
    model = "scripted"

    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self.calls: list[tuple[str, list[Message], type[BaseModel]]] = []

    async def complete[T: BaseModel](
        self,
        *,
        system: str,
        messages: list[Message],
        schema: type[T],
        max_tokens: int,
        timeout_seconds: float,
    ) -> Completion[T]:
        self.calls.append((system, messages, schema))
        value = await self._responder(system, messages, schema)
        if not isinstance(value, schema):
            raise ModelError("invalid_response", "Scripted answer has the wrong type.")
        prompt = system + "".join(m.content for m in messages)
        return Completion(
            value, Usage(_estimate_tokens(prompt), _estimate_tokens(value.model_dump_json()))
        )


def key_terms(question: str) -> str:
    words = re.findall(r"[\w-]+", question)
    return " ".join(w for w in words if w.lower() not in _STOP_WORDS)


async def _keyless_answer(
    system: str, messages: list[Message], schema: type[BaseModel]
) -> BaseModel:
    question = messages[-1].content
    fields = schema.model_fields
    if "queries" in fields:
        terms = key_terms(question)
        queries = [question] + ([terms] if terms and terms != question else [])
        return schema.model_validate({"queries": queries})
    raise ModelError("configuration", f"The keyless model cannot produce {schema.__name__}.")


class KeylessChatModel(ScriptedChatModel):
    provider = "fake"
    model = "keyless-v1"

    def __init__(self) -> None:
        super().__init__(cast(Any, _keyless_answer))
