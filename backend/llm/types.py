"""Shared types for talking to language models.

Every model call returns a Pydantic object validated against the schema the
caller asked for, plus the tokens it used. Every failure is a ``ModelError`` with
a ``category``, so callers decide on retries by category rather than by parsing
provider-specific messages.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel

ErrorCategory = Literal[
    "timeout",  # no answer in time
    "rate_limited",  # provider asked us to slow down
    "unavailable",  # provider down or overloaded (5xx, connection failure)
    "invalid_response",  # answered, but not in the requested shape
    "bad_request",  # our request was rejected; retrying will not help
    "auth",  # missing or wrong API key
    "configuration",  # the system is not set up to call this model
]

RETRYABLE: frozenset[ErrorCategory] = frozenset(
    {"timeout", "rate_limited", "unavailable", "invalid_response"}
)


class ModelError(Exception):
    def __init__(self, category: ErrorCategory, detail: str) -> None:
        super().__init__(f"{category}: {detail}")
        self.category: ErrorCategory = category
        self.detail = detail

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class Completion[T: BaseModel]:
    value: T
    usage: Usage


class ChatModel(Protocol):
    provider: str
    model: str

    async def complete[T: BaseModel](
        self,
        *,
        system: str,
        messages: list[Message],
        schema: type[T],
        max_tokens: int,
        timeout_seconds: float,
    ) -> Completion[T]: ...
