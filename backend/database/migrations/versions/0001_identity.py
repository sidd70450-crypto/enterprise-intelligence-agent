"""Identity, access and audit tables, with the four built-in roles.

Revision ID: 0001
Revises:
Create Date: 2026-11-16
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Permission strings are "<resource>:<action>". Later milestones add the
# resources they introduce (documents, tasks) in their own migrations.
ROLES: dict[str, list[str]] = {
    "owner": ["org:manage", "members:read", "members:write", "audit:read"],
    "admin": ["members:read", "members:write", "audit:read"],
    "analyst": [],
    "viewer": [],
}


def _timestamp(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(120), nullable=False),
        _timestamp("created_at"),
        sa.UniqueConstraint("slug", name="uq_organizations_slug"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
    )
    roles = op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("permissions", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("name", name="uq_roles_name"),
    )
    op.create_table(
        "memberships",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_memberships_user_id_users"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organizations.id",
                ondelete="CASCADE",
                name="fk_memberships_org_id_organizations",
            ),
            nullable=False,
        ),
        sa.Column(
            "role_id",
            sa.Uuid(),
            sa.ForeignKey("roles.id", ondelete="RESTRICT", name="fk_memberships_role_id_roles"),
            nullable=False,
        ),
        _timestamp("created_at"),
        sa.UniqueConstraint("user_id", "org_id", name="uq_memberships_user_id_org_id"),
    )
    op.create_index("ix_memberships_org_id", "memberships", ["org_id"])
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_refresh_tokens_user_id_users"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organizations.id",
                ondelete="CASCADE",
                name="fk_refresh_tokens_org_id_organizations",
            ),
            nullable=False,
        ),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "replaced_by_id",
            sa.Uuid(),
            sa.ForeignKey(
                "refresh_tokens.id",
                ondelete="SET NULL",
                name="fk_refresh_tokens_replaced_by_id_refresh_tokens",
            ),
            nullable=True,
        ),
        _timestamp("created_at"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=True),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("details", postgresql.JSONB(), server_default="{}", nullable=False),
        _timestamp("created_at"),
    )
    op.create_index("ix_audit_events_org_id_created_at", "audit_events", ["org_id", "created_at"])

    op.bulk_insert(
        roles,
        [{"id": uuid.uuid4(), "name": name, "permissions": perms} for name, perms in ROLES.items()],
    )


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("refresh_tokens")
    op.drop_table("memberships")
    op.drop_table("roles")
    op.drop_table("users")
    op.drop_table("organizations")
