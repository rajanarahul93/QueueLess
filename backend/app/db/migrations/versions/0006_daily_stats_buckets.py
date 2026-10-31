"""daily_stats: a surrogate key, NULL service (walk-ins that never started) and NULL hour (the
whole-day row), avg_predicted_wait_sec, and one unique key with NULLS NOT DISTINCT.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

KEY = ["business_id", "service_id", "stat_date", "hour"]


def upgrade() -> None:
    op.drop_constraint("pk_daily_stats", "daily_stats", type_="primary")
    op.add_column(
        "daily_stats",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
    )
    op.create_primary_key("pk_daily_stats", "daily_stats", ["id"])
    op.alter_column("daily_stats", "service_id", nullable=True)
    op.alter_column("daily_stats", "hour", nullable=True)
    op.add_column("daily_stats", sa.Column("avg_predicted_wait_sec", sa.Integer(), nullable=True))
    op.create_index(
        "uq_daily_stats_key", "daily_stats", KEY, unique=True, postgresql_nulls_not_distinct=True
    )


def downgrade() -> None:
    # Refuse rather than delete rollup rows the old key cannot hold.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM daily_stats WHERE service_id IS NULL OR hour IS NULL) THEN
                RAISE EXCEPTION 'daily_stats has rows with no service or no hour; '
                    'refusing to restore the old primary key';
            END IF;
        END $$;
        """
    )
    op.drop_index("uq_daily_stats_key", table_name="daily_stats")
    op.drop_column("daily_stats", "avg_predicted_wait_sec")
    op.alter_column("daily_stats", "hour", nullable=False)
    op.alter_column("daily_stats", "service_id", nullable=False)
    op.drop_constraint("pk_daily_stats", "daily_stats", type_="primary")
    op.drop_column("daily_stats", "id")
    op.create_primary_key("pk_daily_stats", "daily_stats", KEY)
