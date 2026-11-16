"""A scripted model for tests and for running the system without an API key.

``ScriptedChatModel`` answers each request by calling a function you supply, so
tests control exactly what the "model" says, including failures.

``KeylessChatModel`` is what ``llm_provider = "fake"`` gives you: it produces
mechanical answers (for query planning, the question itself plus its key terms)
so the whole pipeline can be exercised end to end: it quotes the first sentence
of each passage, turns each quote into a claim, and marks a claim supported only
if its text appears verbatim in a cited quote. It does no reasoning; results from
it say nothing about the quality of a real model.
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


_SOURCE = re.compile(r'<source id="(S\d+)"[^>]*>\n(.*?)\n</source>', re.S)
_EVIDENCE_LINE = re.compile(r'^\[(E\d+)\] "(.*)" \(', re.M)
_CLAIM = re.compile(r"^Claim (\d+): (.*)\nQuotes:\n((?:  - .*\n?)+)", re.M)
_SENTENCE = re.compile(r".+?[.!?](?=\s|$)", re.S)
KEYLESS_QUOTES = 5


def _first_sentence(text: str) -> str:
    match = _SENTENCE.search(text.strip())
    return (match.group(0) if match else text.strip())[:300]


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


async def _keyless_answer(
    system: str, messages: list[Message], schema: type[BaseModel]
) -> BaseModel:
    """Mechanical but truthful answers for each request type the pipeline makes."""
    content = messages[-1].content
    fields = schema.model_fields
    if "queries" in fields:
        terms = key_terms(content)
        queries = [content] + ([terms] if terms and terms != content else [])
        return schema.model_validate({"queries": queries})
    if "quotes" in fields:  # evidence: the first sentence of each passage
        quotes = [
            {"source_id": label, "quote": _first_sentence(text)}
            for label, text in _SOURCE.findall(content)[:KEYLESS_QUOTES]
        ]
        return schema.model_validate({"quotes": quotes})
    if "claims" in fields:  # report: each quote becomes a claim citing itself
        evidence = _EVIDENCE_LINE.findall(content)
        return schema.model_validate(
            {
                "summary": f"Keyless stand-in model: {len(evidence)} quoted passages, not a "
                "written answer. Configure a real model for actual reports.",
                "claims": [{"text": quote, "evidence_ids": [eid]} for eid, quote in evidence],
                "not_found": None,
            }
        )
    if "verdicts" in fields:  # verification: supported only if the claim is a cited quote
        verdicts = []
        for number, claim, quote_block in _CLAIM.findall(content):
            cited = [line.strip()[3:-1] for line in quote_block.strip().splitlines()]
            found = any(_normalize(claim) in _normalize(q) for q in cited)
            outcome = "appears verbatim in a cited quote." if found else "is not in its quotes."
            verdicts.append(
                {
                    "claim_number": int(number),
                    "verdict": "supported" if found else "unsupported",
                    "rationale": f"Keyless check: the claim text {outcome}",
                }
            )
        return schema.model_validate({"verdicts": verdicts})
    raise ModelError("configuration", f"The keyless model cannot produce {schema.__name__}.")


class KeylessChatModel(ScriptedChatModel):
    provider = "fake"
    model = "keyless-v1"

    def __init__(self) -> None:
        super().__init__(cast(Any, _keyless_answer))
