"""Research tasks: submit, inspect, cancel and follow progress.

``POST /tasks`` stores the task and returns 202; the worker runs it. A retried
submission with the same ``Idempotency-Key`` returns the task already created
instead of starting a second, paid run.

``GET /tasks/{id}/events`` is a server-sent event stream. It starts with a
snapshot from the database, so a client that connects late misses nothing, then
forwards live events until the task ends. The stream needs the bearer token in
the ``Authorization`` header, so browsers read it with ``fetch``, not
``EventSource`` (which cannot send headers).
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.database.documents import Collection
from backend.database.session import get_session
from backend.database.tasks import TERMINAL, ModelCall, Task, TaskStatus, WorkflowStep
from backend.orchestration.events import channel
from backend.orchestration.stages import WORKFLOWS
from backend.reporting.markdown import render_report
from backend.security.audit import record_event
from backend.security.dependencies import Principal, get_settings_from_app, require_permission

router = APIRouter(tags=["tasks"])

Reader = Annotated[Principal, Depends(require_permission("tasks:read"))]
Writer = Annotated[Principal, Depends(require_permission("tasks:write"))]
Session = Annotated[AsyncSession, Depends(get_session)]

KEEPALIVE_SECONDS = 15.0
MAX_STREAM_SECONDS = 3600.0


class TaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=3, max_length=2000)
    collection_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    workflow: str = "pipeline"


class StepOut(BaseModel):
    stage: str
    ordinal: int
    attempt: int
    duration_ms: int
    finished_at: datetime


class UsageOut(BaseModel):
    model_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


class TaskOut(BaseModel):
    id: uuid.UUID
    question: str
    collection_ids: list[uuid.UUID]
    workflow: str
    status: TaskStatus
    cancel_requested: bool
    error_category: str | None
    error_detail: str | None
    attempts: int
    tokens_used: int
    token_budget: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TaskDetail(TaskOut):
    steps: list[StepOut]
    usage: UsageOut
    result: dict[str, Any] | None


def _summary(task: Task) -> TaskOut:
    return TaskOut.model_validate(task, from_attributes=True)


async def _task(session: AsyncSession, principal: Principal, task_id: uuid.UUID) -> Task:
    task = await session.scalar(
        select(Task).where(Task.id == task_id, Task.org_id == principal.org_id)
    )
    if task is None:
        raise HTTPException(404, "Task not found.")
    return task


@router.post("/tasks", status_code=status.HTTP_202_ACCEPTED, response_model=TaskOut)
async def create_task(
    body: TaskIn,
    principal: Writer,
    session: Session,
    settings: Annotated[Settings, Depends(get_settings_from_app)],
    idempotency_key: Annotated[str | None, Header(max_length=100)] = None,
) -> Response:
    if body.workflow not in WORKFLOWS:
        raise HTTPException(422, f"Unknown workflow. Available: {', '.join(sorted(WORKFLOWS))}.")
    requested = set(body.collection_ids)
    owned = await session.scalar(
        select(func.count())
        .select_from(Collection)
        .where(Collection.id.in_(requested), Collection.org_id == principal.org_id)
    )
    if owned != len(requested):
        raise HTTPException(404, "One or more collections were not found.")

    if idempotency_key:
        existing = await _by_key(session, principal, idempotency_key)
        if existing is not None:
            return _replay(existing, body)

    task = Task(
        id=uuid.uuid4(),
        org_id=principal.org_id,
        user_id=principal.user_id,
        question=body.question,
        collection_ids=sorted(requested),
        workflow=body.workflow,
        status=TaskStatus.QUEUED,
        idempotency_key=idempotency_key,
        token_budget=settings.task_token_budget,
    )
    session.add(task)
    record_event(
        session,
        "task.created",
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        resource_type="task",
        resource_id=str(task.id),
    )
    try:
        await session.commit()
    except IntegrityError:
        # A concurrent request with the same key won the race.
        await session.rollback()
        existing = await _by_key(session, principal, idempotency_key or "")
        if existing is None:
            raise
        return _replay(existing, body)
    await session.refresh(task)
    return JSONResponse(_summary(task).model_dump(mode="json"), status_code=202)


async def _by_key(session: AsyncSession, principal: Principal, key: str) -> Task | None:
    return await session.scalar(
        select(Task).where(
            Task.org_id == principal.org_id,
            Task.user_id == principal.user_id,
            Task.idempotency_key == key,
        )
    )


def _replay(existing: Task, body: TaskIn) -> Response:
    same = existing.question == body.question and set(existing.collection_ids) == set(
        body.collection_ids
    )
    if not same:
        raise HTTPException(409, "This Idempotency-Key was already used for a different task.")
    return JSONResponse(_summary(existing).model_dump(mode="json"), status_code=200)


@router.get("/tasks")
async def list_tasks(
    principal: Reader, session: Session, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> list[TaskOut]:
    rows = await session.scalars(
        select(Task)
        .where(Task.org_id == principal.org_id)
        .order_by(Task.created_at.desc(), Task.id)
        .limit(limit)
    )
    return [_summary(task) for task in rows]


@router.get("/tasks/{task_id}")
async def get_task(task_id: uuid.UUID, principal: Reader, session: Session) -> TaskDetail:
    task = await _task(session, principal, task_id)
    steps = await session.scalars(
        select(WorkflowStep).where(WorkflowStep.task_id == task.id).order_by(WorkflowStep.ordinal)
    )
    calls, tokens_in, tokens_out, cost = (
        await session.execute(
            select(
                func.count(ModelCall.id),
                func.coalesce(func.sum(ModelCall.input_tokens), 0),
                func.coalesce(func.sum(ModelCall.output_tokens), 0),
                func.sum(ModelCall.cost_usd),
            ).where(ModelCall.task_id == task.id)
        )
    ).one()
    return TaskDetail(
        **_summary(task).model_dump(),
        steps=[
            StepOut(
                stage=s.stage,
                ordinal=s.ordinal,
                attempt=s.attempt,
                duration_ms=s.duration_ms,
                finished_at=s.created_at,
            )
            for s in steps
        ],
        usage=UsageOut(
            model_calls=calls,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_usd=float(cost) if cost is not None else None,
        ),
        result=task.result,
    )


@router.post("/tasks/{task_id}/cancel", responses={409: {"description": "Task already finished"}})
async def cancel_task(task_id: uuid.UUID, principal: Writer, session: Session) -> TaskOut:
    task = await _task(session, principal, task_id)
    if task.status in TERMINAL:
        raise HTTPException(409, f"The task is already {task.status.value}.")
    # A queued task is cancelled at once; a running one stops at the next stage.
    await session.execute(
        update(Task)
        .where(Task.id == task.id, Task.status == TaskStatus.QUEUED)
        .values(status=TaskStatus.CANCELLED, finished_at=func.now())
    )
    await session.execute(update(Task).where(Task.id == task.id).values(cancel_requested=True))
    record_event(
        session,
        "task.cancel_requested",
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        resource_type="task",
        resource_id=str(task.id),
    )
    await session.commit()
    await session.refresh(task)
    return _summary(task)


@router.get(
    "/tasks/{task_id}/report.md",
    response_class=PlainTextResponse,
    responses={409: {"description": "The task has not completed"}},
)
async def export_report(task_id: uuid.UUID, principal: Reader, session: Session) -> Response:
    task = await _task(session, principal, task_id)
    report = (task.result or {}).get("assemble_report")
    if task.status != TaskStatus.COMPLETED or report is None:
        raise HTTPException(409, "The task has no finished report.")
    return PlainTextResponse(
        render_report(task.question, report),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="report-{task.id}.md"'},
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.get(
    "/tasks/{task_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def task_events(
    task_id: uuid.UUID, request: Request, principal: Reader, session: Session
) -> StreamingResponse:
    await _task(session, principal, task_id)  # 404 before the stream opens
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    redis = request.app.state.redis
    return StreamingResponse(
        _stream(task_id, sessions, redis),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def _snapshot(
    sessions: async_sessionmaker[AsyncSession], task_id: uuid.UUID
) -> dict[str, Any]:
    async with sessions() as session:
        task = await session.get(Task, task_id)
        stages = (
            await session.scalars(
                select(WorkflowStep.stage)
                .where(WorkflowStep.task_id == task_id)
                .order_by(WorkflowStep.ordinal)
            )
        ).all()
    if task is None:
        return {"status": "deleted", "completed_stages": []}
    return {"status": task.status.value, "completed_stages": list(stages)}


async def _stream(
    task_id: uuid.UUID, sessions: async_sessionmaker[AsyncSession], redis: Any
) -> AsyncIterator[str]:
    pubsub = redis.pubsub()
    # Subscribe before taking the snapshot, so no event falls between the two.
    await pubsub.subscribe(channel(task_id))
    try:
        snapshot = await _snapshot(sessions, task_id)
        yield _sse("snapshot", snapshot)
        if snapshot["status"] not in {"queued", "running"}:
            return
        deadline = asyncio.get_running_loop().time() + MAX_STREAM_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=KEEPALIVE_SECONDS
            )
            if message is None:
                yield ": keepalive\n\n"
                continue
            event = json.loads(message["data"])
            yield _sse(event.get("type", "message"), event)
            if event.get("type") == "task" and event.get("status") in {s.value for s in TERMINAL}:
                return
    finally:
        await pubsub.unsubscribe(channel(task_id))
        await pubsub.aclose()
