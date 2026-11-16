"""Background worker: ``python -m backend.worker``.

Processes queued documents and research tasks until it receives SIGTERM or
SIGINT, then finishes the current job and exits. Documents go first: a task can
only use documents that are already indexed. Run more than one worker for more
throughput; they coordinate through the database.
"""

import asyncio
import signal

import structlog
from pydantic import BaseModel
from redis.asyncio import Redis

from backend.config import Settings, get_settings
from backend.database.session import create_engine, create_sessionmaker
from backend.ingestion.embedding import build_embedder
from backend.ingestion.pipeline import IngestionPipeline
from backend.ingestion.storage import FileStorage
from backend.llm.client import build_chat_model
from backend.llm.types import ChatModel, Completion, Message, ModelError
from backend.logging_setup import configure_logging
from backend.orchestration.engine import TaskRunner
from backend.orchestration.events import redis_publisher
from backend.orchestration.stages import WORKFLOWS
from backend.orchestration.workflow import Services
from backend.retrieval.rerank import build_reranker

log = structlog.get_logger("eia.worker")

# Pause after an unexpected error before claiming again, so a persistent fault
# (database down) does not become a tight loop.
ERROR_BACKOFF_SECONDS = 5.0


class UnconfiguredChatModel:
    """Stands in when no model is configured, so tasks fail with a clear reason."""

    provider = "none"
    model = "none"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def complete[T: BaseModel](
        self,
        *,
        system: str,
        messages: list[Message],
        schema: type[T],
        max_tokens: int,
        timeout_seconds: float,
    ) -> Completion[T]:
        raise ModelError("configuration", self._reason)


def _chat_model(settings: Settings) -> ChatModel:
    try:
        return build_chat_model(settings)
    except ModelError as exc:
        log.warning("llm_not_configured", reason=exc.detail)
        return UnconfiguredChatModel(exc.detail)


async def run(settings: Settings) -> None:
    configure_logging(settings.log_level)
    engine = create_engine(settings.database_url)
    sessions = create_sessionmaker(engine)
    redis = Redis.from_url(settings.redis_url)
    # Loading the models can take seconds and downloads them on first run.
    embedder = await asyncio.to_thread(build_embedder, settings)
    reranker = await asyncio.to_thread(build_reranker, settings)
    ingestion = IngestionPipeline(sessions, FileStorage(settings.storage_dir), embedder, settings)
    model = _chat_model(settings)
    tasks = TaskRunner(
        Services(sessions, embedder, reranker, settings),
        model,
        WORKFLOWS,
        publish=redis_publisher(redis),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)

    log.info("worker_started", embedder=embedder.name, llm=f"{model.provider}/{model.model}")
    try:
        while not stop.is_set():
            try:
                if await ingestion.run_once() or await tasks.run_once():
                    continue
                pause = settings.worker_poll_seconds
            except Exception:
                # One bad job or a database blip must not kill the worker.
                log.exception("worker_iteration_failed")
                pause = ERROR_BACKOFF_SECONDS
            try:
                await asyncio.wait_for(stop.wait(), timeout=pause)
            except TimeoutError:
                pass
    finally:
        await redis.aclose()
        await engine.dispose()
        log.info("worker_stopped")


def main() -> None:
    asyncio.run(run(get_settings()))


if __name__ == "__main__":
    main()
