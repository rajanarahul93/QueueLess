"""audit_log.actor_id nullable: NULL means the system (worker jobs).

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("audit_log", "actor_id", nullable=True)


def downgrade() -> None:
    # Refuse rather than delete audit history written by the system.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM audit_log WHERE actor_id IS NULL) THEN
                RAISE EXCEPTION 'audit_log has rows written by the system (actor_id IS NULL); '
                    'refusing to restore NOT NULL';
            END IF;
        END $$;
        """
    )
    op.alter_column("audit_log", "actor_id", nullable=False)
