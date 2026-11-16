"""Research tasks, their stage checkpoints and their model calls (milestone M4).

Like documents, a task row is its own job: workers claim ``queued`` tasks with
``FOR UPDATE SKIP LOCKED`` and a lock token. Each finished stage writes one
``workflow_steps`` row holding its output. That row is the checkpoint: a worker
resuming the task loads finished stages from it instead of running them again.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import documents as _documents  # noqa: F401  (FK targets)
from backend.database.base import Base, created_at, uuid_pk


class TaskStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("org_id", "user_id", "idempotency_key"),
        Index("ix_tasks_status_created_at", "status", "created_at"),
        Index("ix_tasks_org_id_created_at", "org_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    question: Mapped[str] = mapped_column(Text)
    collection_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid))
    workflow: Mapped[str] = mapped_column(String(32))
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status", values_callable=lambda e: [m.value for m in e]),
        default=TaskStatus.QUEUED,
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    token_budget: Mapped[int] = mapped_column(Integer)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    error_category: Mapped[str | None] = mapped_column(String(32))
    error_detail: Mapped[str | None] = mapped_column(String(500))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_token: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowStep(Base):
    """A finished stage of a task: its output is the checkpoint."""

    __tablename__ = "workflow_steps"
    __table_args__ = (UniqueConstraint("task_id", "stage"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(String(32))
    ordinal: Mapped[int] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer)  # the task attempt that finished it
    output: Mapped[dict[str, Any]] = mapped_column(JSONB)
    duration_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at()


class ModelCall(Base):
    """One attempt to call a model, successful or not."""

    __tablename__ = "model_calls"

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6, asdecimal=False))
    latency_ms: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    error_category: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = created_at()
