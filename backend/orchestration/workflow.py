"""What a workflow is: an ordered list of stages, each producing JSON output.

A stage receives the outputs of every earlier stage and returns its own. The
engine stores each output as a checkpoint, so a stage must not depend on
anything it did not receive in ``StageContext``: on resume, earlier stages are
loaded from checkpoints, not run again.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.ingestion.embedding import Embedder
from backend.llm.client import LLMClient
from backend.retrieval.rerank import Reranker


@dataclass(frozen=True)
class TaskInput:
    task_id: uuid.UUID
    org_id: uuid.UUID
    question: str
    collection_ids: list[uuid.UUID]


@dataclass(frozen=True)
class Services:
    sessions: async_sessionmaker[AsyncSession]
    embedder: Embedder
    reranker: Reranker | None
    settings: Settings


@dataclass(frozen=True)
class StageContext:
    task: TaskInput
    outputs: dict[str, dict[str, Any]]  # stage name -> output, for earlier stages
    llm: LLMClient
    services: Services


class Stage(Protocol):
    name: str

    async def run(self, ctx: StageContext) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Workflow:
    name: str
    stages: Sequence[Stage]

    def result(self, outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """The task result: every stage's output, in order."""
        return {stage.name: outputs[stage.name] for stage in self.stages}
