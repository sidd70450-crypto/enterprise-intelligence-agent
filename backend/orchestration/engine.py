"""Runs research tasks stage by stage, with a checkpoint after each stage.

A worker claims a queued task (``FOR UPDATE SKIP LOCKED``, fresh lock token),
loads any checkpoints already stored for it, and runs only the stages that have
none. After each stage it writes the checkpoint, the task's token count and a
heartbeat in one transaction, guarded by the lock token. So:

- a worker that crashes mid-task loses at most the stage it was running; the
  next worker to claim the stale task skips every finished stage;
- a worker that lost its claim (it went quiet and another took over) cannot
  write anything, because its token no longer matches;
- cancellation is checked between stages.

Failures are sorted by kind. A model error that retrying cannot fix (bad
request, missing key) or a used-up token budget fails the task at once. A
transient error returns the task to the queue, up to ``task_max_attempts``.
"""

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.database.tasks import ModelCall, Task, TaskStatus, WorkflowStep
from backend.llm.client import BudgetExceededError, CallRecord, LLMClient
from backend.llm.types import ChatModel, ModelError
from backend.orchestration.events import Publisher, no_events
from backend.orchestration.workflow import Services, StageContext, TaskInput, Workflow

log = structlog.get_logger("eia.orchestration")


class LostLockError(Exception):
    """Another worker owns this task now, or it was deleted."""


class TaskCancelledError(Exception):
    """The user asked to cancel this task."""


@dataclass(frozen=True)
class TaskJob:
    task: TaskInput
    workflow: str
    attempt: int
    token: uuid.UUID
    tokens_used: int
    token_budget: int


async def claim_next_task(session: AsyncSession, settings: Settings) -> TaskJob | None:
    stale_before = datetime.now(UTC) - timedelta(seconds=settings.task_stale_after_seconds)
    next_id = (
        select(Task.id)
        .where(
            or_(
                Task.status == TaskStatus.QUEUED,
                and_(Task.status == TaskStatus.RUNNING, Task.locked_at < stale_before),
            ),
            Task.cancel_requested.is_(False),
        )
        .order_by(Task.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    token = uuid.uuid4()
    task = (
        await session.execute(
            update(Task)
            .where(Task.id == next_id)
            .values(
                status=TaskStatus.RUNNING,
                locked_at=func.now(),
                lock_token=token,
                attempts=Task.attempts + 1,
                started_at=func.coalesce(Task.started_at, func.now()),
            )
            .returning(Task)
        )
    ).scalar_one_or_none()
    await session.commit()
    if task is None:
        return None
    return TaskJob(
        task=TaskInput(task.id, task.org_id, task.question, list(task.collection_ids)),
        workflow=task.workflow,
        attempt=task.attempts,
        token=token,
        tokens_used=task.tokens_used,
        token_budget=task.token_budget,
    )


class TaskRunner:
    def __init__(
        self,
        services: Services,
        model: ChatModel,
        workflows: dict[str, Workflow],
        publish: Publisher = no_events,
    ) -> None:
        self._services = services
        self._model = model
        self._workflows = workflows
        self._publish = publish

    @property
    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        return self._services.sessions

    async def run_once(self) -> bool:
        """Claim and run one task. False when there is nothing to do."""
        async with self._sessions() as session:
            job = await claim_next_task(session, self._services.settings)
        if job is None:
            return False
        structlog.contextvars.bind_contextvars(task_id=str(job.task.task_id))
        try:
            await self.process(job)
        finally:
            structlog.contextvars.unbind_contextvars("task_id")
        return True

    async def process(self, job: TaskJob) -> None:
        settings = self._services.settings
        workflow = self._workflows.get(job.workflow)
        if workflow is None:
            await self._fail(job, "configuration", f"Unknown workflow {job.workflow!r}.")
            return
        if job.attempt > settings.task_max_attempts:
            await self._fail(job, "too_many_attempts", "The task failed repeatedly.")
            return

        current_stage = "setup"
        llm = LLMClient(
            self._model,
            recorder=lambda record: self._record_call(job, current_stage, record),
            token_budget=job.token_budget,
            tokens_used=job.tokens_used,
            max_attempts=settings.llm_max_attempts,
            timeout_seconds=settings.llm_timeout_seconds,
            input_price_per_mtok=settings.llm_input_price_per_mtok,
            output_price_per_mtok=settings.llm_output_price_per_mtok,
        )
        await self._publish(job.task.task_id, {"type": "task", "status": "running"})
        try:
            outputs = await self._checkpoints(job)
            for ordinal, stage in enumerate(workflow.stages):
                if stage.name in outputs:
                    log.info("stage_skipped", stage=stage.name, reason="checkpoint")
                    continue
                await self._check_still_ours(job)
                current_stage = stage.name
                await self._publish(
                    job.task.task_id, {"type": "stage", "stage": stage.name, "status": "started"}
                )
                started = time.perf_counter()
                context = StageContext(job.task, dict(outputs), llm, self._services)
                output = await stage.run(context)
                duration_ms = round((time.perf_counter() - started) * 1000)
                await self._save_checkpoint(job, stage.name, ordinal, output, duration_ms, llm)
                outputs[stage.name] = output
                log.info("stage_completed", stage=stage.name, duration_ms=duration_ms)
                await self._publish(
                    job.task.task_id,
                    {"type": "stage", "stage": stage.name, "status": "completed",
                     "duration_ms": duration_ms},
                )  # fmt: skip
            await self._complete(job, workflow.result(outputs), llm.tokens_used)
        except LostLockError:
            log.warning("task_lock_lost")
        except TaskCancelledError:
            await self._finish(job, TaskStatus.CANCELLED, None, None, llm.tokens_used)
        except BudgetExceededError as exc:
            await self._fail(job, "budget_exceeded", str(exc), llm.tokens_used)
        except ModelError as exc:
            if exc.retryable and job.attempt < settings.task_max_attempts:
                await self._release(job, llm.tokens_used)
            else:
                await self._fail(job, exc.category, _model_error_detail(exc), llm.tokens_used)
        except Exception:
            log.exception("task_error", stage=current_stage, attempt=job.attempt)
            if job.attempt < settings.task_max_attempts:
                await self._release(job, llm.tokens_used)
            else:
                await self._fail(job, "internal_error", "The task failed.", llm.tokens_used)

    # --- persistence -----------------------------------------------------

    async def _checkpoints(self, job: TaskJob) -> dict[str, dict[str, Any]]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(WorkflowStep.stage, WorkflowStep.output).where(
                    WorkflowStep.task_id == job.task.task_id
                )
            )
            return {stage: output for stage, output in rows}

    async def _locked(self, session: AsyncSession, job: TaskJob) -> Task:
        task = await session.scalar(
            select(Task)
            .where(Task.id == job.task.task_id, Task.lock_token == job.token)
            .with_for_update()
        )
        if task is None:
            raise LostLockError
        return task

    async def _check_still_ours(self, job: TaskJob) -> None:
        async with self._sessions() as session:
            task = await self._locked(session, job)
            cancel = task.cancel_requested
            task.locked_at = datetime.now(UTC)  # heartbeat
            await session.commit()
        if cancel:
            raise TaskCancelledError

    async def _save_checkpoint(
        self,
        job: TaskJob,
        stage: str,
        ordinal: int,
        output: dict[str, Any],
        duration_ms: int,
        llm: LLMClient,
    ) -> None:
        async with self._sessions() as session:
            task = await self._locked(session, job)
            session.add(
                WorkflowStep(
                    org_id=job.task.org_id,
                    task_id=job.task.task_id,
                    stage=stage,
                    ordinal=ordinal,
                    attempt=job.attempt,
                    output=output,
                    duration_ms=duration_ms,
                )
            )
            task.tokens_used = llm.tokens_used
            task.locked_at = datetime.now(UTC)
            await session.commit()

    async def _record_call(self, job: TaskJob, stage: str, record: CallRecord) -> None:
        async with self._sessions() as session:
            session.add(
                ModelCall(
                    org_id=job.task.org_id,
                    task_id=job.task.task_id,
                    stage=stage,
                    purpose=record.purpose,
                    provider=record.provider,
                    model=record.model,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    cost_usd=record.cost_usd,
                    latency_ms=record.latency_ms,
                    status=record.status,
                    error_category=record.error_category,
                )
            )
            await session.commit()

    async def _complete(self, job: TaskJob, result: dict[str, Any], tokens: int) -> None:
        await self._finish(job, TaskStatus.COMPLETED, None, None, tokens, result)

    async def _fail(
        self, job: TaskJob, category: str, detail: str, tokens: int | None = None
    ) -> None:
        await self._finish(job, TaskStatus.FAILED, category, detail, tokens)

    async def _finish(
        self,
        job: TaskJob,
        status: TaskStatus,
        category: str | None,
        detail: str | None,
        tokens: int | None,
        result: dict[str, Any] | None = None,
    ) -> None:
        async with self._sessions() as session:
            try:
                task = await self._locked(session, job)
            except LostLockError:
                return
            task.status = status
            task.error_category = category
            task.error_detail = detail[:500] if detail else None
            task.result = result
            task.finished_at = datetime.now(UTC)
            task.lock_token = task.locked_at = None
            if tokens is not None:
                task.tokens_used = tokens
            await session.commit()
        log.info("task_finished", status=status.value, error=category)
        await self._publish(
            job.task.task_id, {"type": "task", "status": status.value, "error_category": category}
        )

    async def _release(self, job: TaskJob, tokens: int) -> None:
        async with self._sessions() as session:
            try:
                task = await self._locked(session, job)
            except LostLockError:
                return
            task.status = TaskStatus.QUEUED
            task.tokens_used = tokens
            task.lock_token = task.locked_at = None
            await session.commit()
        await self._publish(job.task.task_id, {"type": "task", "status": "queued", "retry": True})


def _model_error_detail(error: ModelError) -> str:
    messages = {
        "auth": "The model provider rejected the API key.",
        "configuration": "No language model is configured.",
        "bad_request": "The model provider rejected the request.",
    }
    return messages.get(error.category, "The language model could not complete the request.")
