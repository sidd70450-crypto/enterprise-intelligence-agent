"""Background worker: ``python -m backend.worker``.

Processes queued documents one at a time until it receives SIGTERM or SIGINT,
then finishes the current document and exits. Run more than one for more
throughput; they coordinate through the database.
"""

import asyncio
import signal

import structlog

from backend.config import Settings, get_settings
from backend.database.session import create_engine, create_sessionmaker
from backend.ingestion.embedding import build_embedder
from backend.ingestion.pipeline import IngestionPipeline
from backend.ingestion.storage import FileStorage
from backend.logging_setup import configure_logging

log = structlog.get_logger("eia.worker")


async def run(settings: Settings) -> None:
    configure_logging(settings.log_level)
    engine = create_engine(settings.database_url)
    # Loading the model can take seconds and downloads it on first run.
    embedder = await asyncio.to_thread(build_embedder, settings)
    pipeline = IngestionPipeline(
        create_sessionmaker(engine), FileStorage(settings.storage_dir), embedder, settings
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)

    log.info("worker_started", embedder=embedder.name)
    try:
        while not stop.is_set():
            if await pipeline.run_once():
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
            except TimeoutError:
                pass
    finally:
        await engine.dispose()
        log.info("worker_stopped")


def main() -> None:
    asyncio.run(run(get_settings()))


if __name__ == "__main__":
    main()
