"""Appointments, queue_tokens, service_sessions, outbox_events, daily_stats.

Revision ID: 0003
Revises: 0002
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

ACTIVE_TOKEN = "status IN ('waiting', 'called', 'in_service')"


def uuid_id() -> sa.Column[uuid.UUID]:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def uuid_fk(name: str, fk_name: str, target: str, nullable: bool = False) -> sa.Column[uuid.UUID]:
    return sa.Column(
        name,
        sa.Uuid(),
        sa.ForeignKey(target, name=fk_name, ondelete="RESTRICT"),
        nullable=nullable,
    )


def now_column(name: str) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "appointments",
        uuid_id(),
        uuid_fk("business_id", "fk_appointments_business_id_businesses", "businesses.id"),
        uuid_fk("service_id", "fk_appointments_service_id_services", "services.id"),
        uuid_fk("resource_id", "fk_appointments_resource_id_resources", "resources.id"),
        uuid_fk("customer_id", "fk_appointments_customer_id_users", "users.id"),
        sa.Column("during", postgresql.TSTZRANGE(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        uuid_fk(
            "rescheduled_from_id",
            "fk_appointments_rescheduled_from_id_appointments",
            "appointments.id",
            nullable=True,
        ),
        now_column("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_appointments"),
        sa.CheckConstraint(
            "status IN ('booked', 'checked_in', 'in_service', 'completed', 'cancelled', "
            "'no_show', 'rescheduled')",
            name=op.f("ck_appointments_status"),
        ),
        sa.CheckConstraint("priority BETWEEN 0 AND 2", name=op.f("ck_appointments_priority_range")),
        sa.CheckConstraint(
            "lower_inc(during) AND NOT upper_inc(during) AND NOT isempty(during) "
            "AND NOT lower_inf(during) AND NOT upper_inf(during)",
            name=op.f("ck_appointments_range_valid"),
        ),
    )
    op.execute(
        "ALTER TABLE appointments ADD CONSTRAINT no_overlap "
        "EXCLUDE USING gist (resource_id WITH =, during WITH &&) "
        "WHERE (status IN ('booked', 'checked_in', 'in_service'))"
    )
    op.create_index("ix_appointments_customer_id_status", "appointments", ["customer_id", "status"])

    op.create_table(
        "queue_tokens",
        uuid_id(),
        uuid_fk("business_id", "fk_queue_tokens_business_id_businesses", "businesses.id"),
        uuid_fk("queue_id", "fk_queue_tokens_queue_id_queues", "queues.id"),
        sa.Column("token_no", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        uuid_fk(
            "appointment_id",
            "fk_queue_tokens_appointment_id_appointments",
            "appointments.id",
            nullable=True,
        ),
        uuid_fk("customer_id", "fk_queue_tokens_customer_id_users", "users.id"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("skip_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        now_column("joined_at"),
        sa.Column("called_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        now_column("queued_at"),
        sa.Column("predicted_wait_sec", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_queue_tokens"),
        sa.UniqueConstraint("queue_id", "token_no", name="uq_token"),
        sa.UniqueConstraint("appointment_id", name="uq_queue_tokens_appointment_id"),
        sa.CheckConstraint(
            "source IN ('walk_in', 'appointment')", name=op.f("ck_queue_tokens_source")
        ),
        sa.CheckConstraint(
            "status IN ('waiting', 'called', 'in_service', 'completed', 'no_show', 'cancelled')",
            name=op.f("ck_queue_tokens_status"),
        ),
        sa.CheckConstraint("priority BETWEEN 0 AND 2", name=op.f("ck_queue_tokens_priority_range")),
    )
    op.execute(
        f"CREATE UNIQUE INDEX one_active_token ON queue_tokens (queue_id, customer_id) "
        f"WHERE {ACTIVE_TOKEN}"
    )
    op.execute(
        f"CREATE INDEX active_tokens ON queue_tokens (queue_id, priority DESC, queued_at) "
        f"WHERE {ACTIVE_TOKEN}"
    )

    op.create_table(
        "service_sessions",
        uuid_id(),
        uuid_fk("business_id", "fk_service_sessions_business_id_businesses", "businesses.id"),
        uuid_fk("token_id", "fk_service_sessions_token_id_queue_tokens", "queue_tokens.id"),
        uuid_fk("service_id", "fk_service_sessions_service_id_services", "services.id"),
        uuid_fk("resource_id", "fk_service_sessions_resource_id_resources", "resources.id"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_service_sessions"),
    )
    op.create_index(
        "ix_service_sessions_service_id_ended_at", "service_sessions", ["service_id", "ended_at"]
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("queue_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        now_column("created_at"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
    )
    op.create_index(
        "ix_outbox_unpublished",
        "outbox_events",
        ["id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_index("ix_outbox_queue_id_id", "outbox_events", ["queue_id", "id"])

    op.create_table(
        "daily_stats",
        uuid_fk("business_id", "fk_daily_stats_business_id_businesses", "businesses.id"),
        uuid_fk("service_id", "fk_daily_stats_service_id_services", "services.id"),
        sa.Column("stat_date", sa.Date(), nullable=False),
        sa.Column("hour", sa.SmallInteger(), nullable=False),
        sa.Column("tokens_served", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("avg_wait_sec", sa.Integer(), nullable=True),
        sa.Column("p90_wait_sec", sa.Integer(), nullable=True),
        sa.Column("no_shows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("tokens_joined", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cancelled", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint(
            "business_id", "service_id", "stat_date", "hour", name="pk_daily_stats"
        ),
        sa.CheckConstraint("hour BETWEEN 0 AND 23", name=op.f("ck_daily_stats_hour_range")),
    )


def downgrade() -> None:
    for table in (
        "daily_stats",
        "outbox_events",
        "service_sessions",
        "queue_tokens",
        "appointments",
    ):
        op.drop_table(table)
