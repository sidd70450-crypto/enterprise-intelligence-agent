"""Research tasks, stage checkpoints, model calls; task permissions.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DOCS = ["documents:read", "documents:write"]
_ADMIN = ["members:read", "members:write", "audit:read"]
PERMISSIONS_BEFORE: dict[str, list[str]] = {
    "owner": ["org:manage", *_ADMIN, *_DOCS],
    "admin": [*_ADMIN, *_DOCS],
    "analyst": _DOCS,
    "viewer": ["documents:read"],
}
_TASKS = ["tasks:read", "tasks:write"]
PERMISSIONS_AFTER: dict[str, list[str]] = {
    "owner": PERMISSIONS_BEFORE["owner"] + _TASKS,
    "admin": PERMISSIONS_BEFORE["admin"] + _TASKS,
    "analyst": PERMISSIONS_BEFORE["analyst"] + _TASKS,
    "viewer": ["documents:read", "tasks:read"],
}

task_status = postgresql.ENUM(
    "queued", "running", "completed", "failed", "cancelled", name="task_status", create_type=False
)


def _timestamp(name: str, nullable: bool = False) -> sa.Column[object]:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _fk(
    column: str, table: str, target: str, ondelete: str, nullable: bool = False
) -> sa.Column[object]:
    referred = target.split(".")[0]
    return sa.Column(
        column,
        sa.Uuid(),
        sa.ForeignKey(target, ondelete=ondelete, name=f"fk_{table}_{column}_{referred}"),
        nullable=nullable,
    )


def _set_permissions(permissions: dict[str, list[str]]) -> None:
    for role, perms in permissions.items():
        op.execute(
            sa.text(
                "UPDATE roles SET permissions = CAST(:perms AS jsonb) WHERE name = :role"
            ).bindparams(perms=json.dumps(perms), role=role)
        )


def upgrade() -> None:
    task_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _fk("org_id", "tasks", "organizations.id", "CASCADE"),
        _fk("user_id", "tasks", "users.id", "SET NULL", nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("collection_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("workflow", sa.String(32), nullable=False),
        sa.Column("status", task_status, nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=True),
        sa.Column("token_budget", sa.Integer(), nullable=False),
        sa.Column("tokens_used", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("error_category", sa.String(32), nullable=True),
        sa.Column("error_detail", sa.String(500), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        _timestamp("locked_at", nullable=True),
        sa.Column("lock_token", sa.Uuid(), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        _timestamp("started_at", nullable=True),
        _timestamp("finished_at", nullable=True),
        sa.UniqueConstraint(
            "org_id", "user_id", "idempotency_key", name="uq_tasks_org_id_user_id_idempotency_key"
        ),
    )
    op.create_index("ix_tasks_status_created_at", "tasks", ["status", "created_at"])
    op.create_index("ix_tasks_org_id_created_at", "tasks", ["org_id", "created_at"])

    op.create_table(
        "workflow_steps",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _fk("org_id", "workflow_steps", "organizations.id", "CASCADE"),
        _fk("task_id", "workflow_steps", "tasks.id", "CASCADE"),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("output", postgresql.JSONB(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        _timestamp("created_at"),
        sa.UniqueConstraint("task_id", "stage", name="uq_workflow_steps_task_id_stage"),
    )

    op.create_table(
        "model_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _fk("org_id", "model_calls", "organizations.id", "CASCADE"),
        _fk("task_id", "model_calls", "tasks.id", "CASCADE"),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_category", sa.String(32), nullable=True),
        _timestamp("created_at"),
    )
    op.create_index("ix_model_calls_task_id", "model_calls", ["task_id"])

    _set_permissions(PERMISSIONS_AFTER)


def downgrade() -> None:
    _set_permissions(PERMISSIONS_BEFORE)
    op.drop_table("model_calls")
    op.drop_table("workflow_steps")
    op.drop_table("tasks")
    task_status.drop(op.get_bind(), checkfirst=True)
