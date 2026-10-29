"""Core tables: users, businesses, business_members, resources, services, queues.

Revision ID: 0002
Revises: 0001
"""

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def uuid_id() -> sa.Column[uuid.UUID]:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def uuid_fk(name: str, fk_name: str, target: str) -> sa.Column[uuid.UUID]:
    return sa.Column(
        name, sa.Uuid(), sa.ForeignKey(target, name=fk_name, ondelete="RESTRICT"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "users",
        uuid_id(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("no_show_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("phone", name="uq_users_phone"),
        sa.CheckConstraint(
            "role IN ('customer', 'staff', 'owner', 'admin')", name=op.f("ck_users_role")
        ),
        sa.CheckConstraint(
            "email IS NOT NULL OR phone IS NOT NULL", name=op.f("ck_users_email_or_phone")
        ),
    )
    op.create_table(
        "businesses",
        uuid_id(),
        uuid_fk("owner_id", "fk_businesses_owner_id_users", "users.id"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("open_hours", postgresql.JSONB(), nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_businesses"),
    )
    op.create_table(
        "business_members",
        uuid_fk("business_id", "fk_business_members_business_id_businesses", "businesses.id"),
        uuid_fk("user_id", "fk_business_members_user_id_users", "users.id"),
        sa.Column("role", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("business_id", "user_id", name="pk_business_members"),
        sa.CheckConstraint("role IN ('owner', 'staff')", name=op.f("ck_business_members_role")),
    )
    op.create_table(
        "resources",
        uuid_id(),
        uuid_fk("business_id", "fk_resources_business_id_businesses", "businesses.id"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_resources"),
    )
    op.create_table(
        "services",
        uuid_id(),
        uuid_fk("business_id", "fk_services_business_id_businesses", "businesses.id"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("duration_min", sa.Integer(), nullable=False),
        sa.Column("buffer_min", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_services"),
        sa.CheckConstraint("duration_min > 0", name=op.f("ck_services_duration_min_positive")),
        sa.CheckConstraint("buffer_min >= 0", name=op.f("ck_services_buffer_min_non_negative")),
    )
    op.create_index("ix_services_business_id_is_active", "services", ["business_id", "is_active"])
    op.create_table(
        "queues",
        uuid_id(),
        uuid_fk("business_id", "fk_queues_business_id_businesses", "businesses.id"),
        uuid_fk("resource_id", "fk_queues_resource_id_resources", "resources.id"),
        sa.Column("queue_date", sa.Date(), nullable=False),
        sa.Column("last_token_no", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_queues"),
        sa.UniqueConstraint("resource_id", "queue_date", name="uq_queues_resource_date"),
        sa.CheckConstraint("status IN ('open', 'paused', 'closed')", name=op.f("ck_queues_status")),
    )


def downgrade() -> None:
    for table in ("queues", "services", "resources", "business_members", "businesses", "users"):
        op.drop_table(table)
