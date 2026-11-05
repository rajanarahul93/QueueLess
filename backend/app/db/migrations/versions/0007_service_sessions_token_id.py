"""Index service_sessions.token_id: the staff snapshot joins a token to its service.

Without it the snapshot's join scans the whole sessions table, which grows by one row per
served token and is never pruned. Plain index, not CONCURRENTLY: the table is small today.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_service_sessions_token_id", "service_sessions", ["token_id"])


def downgrade() -> None:
    op.drop_index("ix_service_sessions_token_id", table_name="service_sessions")
