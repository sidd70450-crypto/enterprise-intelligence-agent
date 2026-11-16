"""Run the parser in a separate, limited process.

A hostile or broken file can make a parsing library loop forever, allocate
gigabytes, or crash the interpreter. Parsing in a child process contains all
three: the child gets a CPU-time and address-space limit, a wall-clock timeout
enforced by the parent, and an environment with every ``EIA_`` variable (the
database URL, the JWT secret) removed. Whatever goes wrong, the worker survives
and the document is marked failed with a reason.

This is containment, not a full sandbox: the child can still read files the
worker user can read. A stronger boundary (a separate container with no network)
is planned with the code-execution sandbox in v0.3.
"""

import asyncio
import json
import os
import resource
import sys
from pathlib import Path

from backend.ingestion.detect import FileKind
from backend.ingestion.parsing import Page, ParseError


def _child_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("EIA_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _worker_command() -> list[str]:
    return ["-m", "backend.ingestion.parse_worker"]


def _limit(cpu_seconds: int, memory_bytes: int) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))


async def parse_in_sandbox(
    path: Path,
    kind: FileKind,
    *,
    max_pdf_pages: int,
    timeout_seconds: float,
    memory_mb: int,
) -> list[Page]:
    request = json.dumps({"path": str(path), "kind": kind, "max_pdf_pages": max_pdf_pages})
    cpu_seconds = int(timeout_seconds) + 1
    memory_bytes = memory_mb * 1024 * 1024
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *_worker_command(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_child_environment(),
        preexec_fn=lambda: _limit(cpu_seconds, memory_bytes),
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            stdout, _stderr = await process.communicate(request.encode())
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ParseError("timeout", "Parsing took too long and was stopped.") from None

    if process.returncode != 0:
        # Killed by the CPU limit, out of memory, or crashed.
        raise ParseError("parser_failed", "The file could not be parsed safely.")
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        raise ParseError("parser_failed", "The parser returned an invalid result.") from None
    if not result["ok"]:
        raise ParseError(str(result["code"]), str(result["detail"]))
    return [Page(page["number"], page["text"]) for page in result["pages"]]
