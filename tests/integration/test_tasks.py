"""Research tasks: the API, the engine's checkpoints, resume, cancel and failure handling."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.database.tasks import ModelCall, Task, TaskStatus, WorkflowStep
from backend.ingestion.embedding import HashingEmbedder
from backend.ingestion.pipeline import IngestionPipeline
from backend.llm.fake import KeylessChatModel, ScriptedChatModel
from backend.llm.types import ChatModel, Message, ModelError
from backend.orchestration.engine import TaskRunner, claim_next_task
from backend.orchestration.events import redis_publisher
from backend.orchestration.stages import WORKFLOWS, PlanQueries, RetrieveSources
from backend.orchestration.workflow import Services, StageContext, Workflow
from backend.worker import UnconfiguredChatModel
from tests.integration.conftest import AddMember, Login, Register, bearer, drain

DOCS = {
    "retention.md": "# Retention policy POL-114\n\nCustomer access logs are kept for 400 days.",
    "travel.md": "# Travel policy\n\nEconomy class for flights shorter than six hours.",
}


@pytest.fixture
async def account(
    client: AsyncClient, register: Register, login: Login, pipeline: IngestionPipeline
) -> dict[str, str]:
    """A user with an indexed collection."""
    info = await register("ada@example.com", org="Org A")
    token = await login("ada@example.com")
    collection = await client.post("/api/v1/collections", json={"name": "C"}, headers=bearer(token))
    for name, text in DOCS.items():
        await client.post(
            f"/api/v1/collections/{collection.json()['id']}/documents",
            files={"file": (name, text.encode())},
            headers=bearer(token),
        )
    await drain(pipeline)
    return {**info, "token": token, "collection_id": collection.json()["id"]}


def _runner(
    app: FastAPI,
    settings: Settings,
    model: ChatModel | None = None,
    workflows: dict[str, Workflow] | None = None,
    publish: bool = False,
) -> TaskRunner:
    services = Services(app.state.sessionmaker, HashingEmbedder(), None, settings)
    return TaskRunner(
        services,
        model or KeylessChatModel(),
        workflows or WORKFLOWS,
        **({"publish": redis_publisher(app.state.redis)} if publish else {}),
    )


async def _submit(
    client: AsyncClient, account: dict[str, str], question: str = "How long are logs kept?",
    key: str | None = None, collections: list[str] | None = None,
) -> Any:  # fmt: skip
    headers = bearer(account["token"]) | ({"Idempotency-Key": key} if key else {})
    return await client.post(
        "/api/v1/tasks",
        json={"question": question, "collection_ids": collections or [account["collection_id"]]},
        headers=headers,
    )


async def _detail(client: AsyncClient, account: dict[str, str], task_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{task_id}", headers=bearer(account["token"]))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_a_task_runs_end_to_end(
    app: FastAPI, client: AsyncClient, account: dict[str, str], settings: Settings
) -> None:
    accepted = await _submit(client, account)
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "queued"

    assert await _runner(app, settings).run_once()
    task = await _detail(client, account, accepted.json()["id"])
    assert task["status"] == "completed", task
    assert [s["stage"] for s in task["steps"]] == ["plan_queries", "retrieve"]
    assert task["result"]["plan_queries"]["queries"][0] == "How long are logs kept?"
    sources = task["result"]["retrieve"]["sources"]
    assert sources[0]["filename"] == "retention.md"
    assert task["usage"]["model_calls"] == 1
    assert task["usage"]["cost_usd"] is None  # no prices configured: no invented cost
    assert task["tokens_used"] == task["usage"]["input_tokens"] + task["usage"]["output_tokens"]


class WorkerKilled(BaseException):
    """Stands in for the process dying: not an Exception, so nothing catches it."""


class CountingPlan(PlanQueries):
    def __init__(self) -> None:
        self.runs = 0

    async def run(self, ctx: StageContext) -> dict[str, Any]:
        self.runs += 1
        return await super().run(ctx)


class DiesOnce:
    name = "middle"

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, ctx: StageContext) -> dict[str, Any]:
        self.runs += 1
        if self.runs == 1:
            raise WorkerKilled
        return {"seen_queries": len(ctx.outputs["plan_queries"]["queries"])}


async def test_a_killed_worker_resumes_without_repeating_finished_stages(
    app: FastAPI,
    client: AsyncClient,
    account: dict[str, str],
    settings: Settings,
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The M4 exit test."""
    plan, middle = CountingPlan(), DiesOnce()
    workflow = Workflow("pipeline", [plan, middle, RetrieveSources()])
    task_id = (await _submit(client, account)).json()["id"]

    # The first worker finishes plan_queries, then dies inside the next stage.
    with pytest.raises(WorkerKilled):
        await _runner(app, settings, workflows={"pipeline": workflow}).run_once()
    async with db() as session:
        assert (await session.scalars(select(WorkflowStep.stage))).all() == ["plan_queries"]
        # Nobody heartbeats for a dead worker; after the stale limit the task is up for grabs.
        stale = datetime.now(UTC) - timedelta(seconds=settings.task_stale_after_seconds + 1)
        await session.execute(update(Task).values(locked_at=stale))
        await session.commit()

    # A second worker picks it up.
    assert await _runner(app, settings, workflows={"pipeline": workflow}).run_once()
    task = await _detail(client, account, task_id)
    assert task["status"] == "completed"
    assert task["attempts"] == 2
    assert plan.runs == 1  # finished stage not repeated
    assert middle.runs == 2  # the interrupted stage ran again
    assert [(s["stage"], s["attempt"]) for s in task["steps"]] == [
        ("plan_queries", 1),
        ("middle", 2),
        ("retrieve", 2),
    ]
    async with db() as session:
        calls = await session.scalar(select(func.count()).select_from(ModelCall))
    assert calls == 1  # the model was not paid twice for the same stage


async def test_a_stale_worker_cannot_write_after_losing_its_claim(
    app: FastAPI,
    client: AsyncClient,
    account: dict[str, str],
    settings: Settings,
    db: async_sessionmaker[AsyncSession],
) -> None:
    await _submit(client, account)
    async with db() as session:
        job = await claim_next_task(session, settings)
        assert job is not None
        await session.execute(update(Task).values(lock_token=uuid.uuid4()))  # taken over
        await session.commit()
    await _runner(app, settings).process(job)
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(WorkflowStep)) == 0
        assert await session.scalar(select(Task.status)) == TaskStatus.RUNNING


async def test_idempotency_key_prevents_a_second_run(
    client: AsyncClient, account: dict[str, str]
) -> None:
    first = await _submit(client, account, key="abc-123")
    again = await _submit(client, account, key="abc-123")
    assert (first.status_code, again.status_code) == (202, 200)
    assert again.json()["id"] == first.json()["id"]

    conflicting = await _submit(client, account, question="A different question?", key="abc-123")
    assert conflicting.status_code == 409

    unkeyed = [await _submit(client, account) for _ in range(2)]
    assert unkeyed[0].json()["id"] != unkeyed[1].json()["id"]


async def test_cancelling_a_queued_task_is_immediate(
    client: AsyncClient, account: dict[str, str]
) -> None:
    task_id = (await _submit(client, account)).json()["id"]
    response = await client.post(
        f"/api/v1/tasks/{task_id}/cancel", headers=bearer(account["token"])
    )
    assert response.json()["status"] == "cancelled"
    again = await client.post(f"/api/v1/tasks/{task_id}/cancel", headers=bearer(account["token"]))
    assert again.status_code == 409


async def test_a_running_task_stops_at_the_next_stage_when_cancelled(
    app: FastAPI,
    client: AsyncClient,
    account: dict[str, str],
    settings: Settings,
) -> None:
    task_id = (await _submit(client, account)).json()["id"]

    class CancelsDuringPlan(PlanQueries):
        async def run(self, ctx: StageContext) -> dict[str, Any]:
            output = await super().run(ctx)
            await client.post(f"/api/v1/tasks/{task_id}/cancel", headers=bearer(account["token"]))
            return output

    workflow = Workflow("pipeline", [CancelsDuringPlan(), RetrieveSources()])
    await _runner(app, settings, workflows={"pipeline": workflow}).run_once()
    task = await _detail(client, account, task_id)
    assert task["status"] == "cancelled"
    assert [s["stage"] for s in task["steps"]] == ["plan_queries"]  # retrieve never ran


async def test_a_missing_model_configuration_fails_the_task_clearly(
    app: FastAPI, client: AsyncClient, account: dict[str, str], settings: Settings
) -> None:
    task_id = (await _submit(client, account)).json()["id"]
    model = UnconfiguredChatModel("EIA_ANTHROPIC_API_KEY is not set.")
    await _runner(app, settings, model=model).run_once()
    task = await _detail(client, account, task_id)
    assert (task["status"], task["error_category"]) == ("failed", "configuration")
    assert task["attempts"] == 1  # not retried: retrying cannot fix configuration


async def test_a_persistent_provider_outage_is_retried_then_failed(
    app: FastAPI, client: AsyncClient, account: dict[str, str], settings: Settings
) -> None:
    async def down(system: str, messages: list[Message], schema: type[BaseModel]) -> BaseModel:
        raise ModelError("unavailable", "HTTP 503")

    task_id = (await _submit(client, account)).json()["id"]
    quick = settings.model_copy(update={"llm_max_attempts": 1})
    runner = _runner(app, quick, model=ScriptedChatModel(down))
    while await runner.run_once():
        pass
    task = await _detail(client, account, task_id)
    assert (task["status"], task["error_category"]) == ("failed", "unavailable")
    assert task["attempts"] == settings.task_max_attempts
    assert "503" not in (task["error_detail"] or "")


async def test_other_organizations_cannot_see_or_touch_tasks(
    client: AsyncClient, account: dict[str, str], register: Register, login: Login
) -> None:
    task_id = (await _submit(client, account)).json()["id"]
    await register("eve@example.com", org="Org E")
    eve = await login("eve@example.com")
    responses = [
        await client.get(f"/api/v1/tasks/{task_id}", headers=bearer(eve)),
        await client.post(f"/api/v1/tasks/{task_id}/cancel", headers=bearer(eve)),
        await client.get(f"/api/v1/tasks/{task_id}/events", headers=bearer(eve)),
        await client.post(
            "/api/v1/tasks",
            json={"question": "Steal this?", "collection_ids": [account["collection_id"]]},
            headers=bearer(eve),
        ),
    ]
    assert [r.status_code for r in responses] == [404, 404, 404, 404]
    assert (await client.get("/api/v1/tasks", headers=bearer(eve))).json() == []


async def test_viewers_can_read_tasks_but_not_create_them(
    client: AsyncClient, account: dict[str, str], add_member: AddMember, login: Login
) -> None:
    await _submit(client, account)
    await add_member("val@example.com", account["organization_id"], "viewer")
    viewer = await login("val@example.com")
    assert len((await client.get("/api/v1/tasks", headers=bearer(viewer))).json()) == 1
    denied = await client.post(
        "/api/v1/tasks",
        json={"question": "May I?", "collection_ids": [account["collection_id"]]},
        headers=bearer(viewer),
    )
    assert denied.status_code == 403


def _events(body: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if not line.startswith(":"))
        if "event" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events


async def test_progress_streams_live_and_ends_when_the_task_does(
    app: FastAPI, client: AsyncClient, account: dict[str, str], settings: Settings
) -> None:
    task_id = (await _submit(client, account)).json()["id"]

    async def follow() -> str:
        response = await client.get(
            f"/api/v1/tasks/{task_id}/events", headers=bearer(account["token"])
        )
        assert response.headers["content-type"].startswith("text/event-stream")
        return response.text

    stream = asyncio.create_task(follow())
    await asyncio.sleep(0.3)  # let the stream subscribe
    await _runner(app, settings, publish=True).run_once()
    events = _events(await asyncio.wait_for(stream, timeout=10))

    assert events[0] == ("snapshot", {"status": "queued", "completed_stages": []})
    stages = [(e["stage"], e["status"]) for kind, e in events if kind == "stage"]
    assert stages == [
        ("plan_queries", "started"), ("plan_queries", "completed"),
        ("retrieve", "started"), ("retrieve", "completed"),
    ]  # fmt: skip
    assert events[-1][0] == "task" and events[-1][1]["status"] == "completed"


async def test_a_late_subscriber_gets_the_final_state_at_once(
    app: FastAPI, client: AsyncClient, account: dict[str, str], settings: Settings
) -> None:
    task_id = (await _submit(client, account)).json()["id"]
    await _runner(app, settings).run_once()
    response = await client.get(f"/api/v1/tasks/{task_id}/events", headers=bearer(account["token"]))
    assert _events(response.text) == [
        ("snapshot", {"status": "completed", "completed_stages": ["plan_queries", "retrieve"]})
    ]


async def test_list_limit_is_validated(client: AsyncClient, account: dict[str, str]) -> None:
    response = await client.get("/api/v1/tasks?limit=1000", headers=bearer(account["token"]))
    assert response.status_code == 422
