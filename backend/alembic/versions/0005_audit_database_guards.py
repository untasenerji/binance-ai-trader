"""add database-level audit append-only guards

Revision ID: 0005_audit_database_guards
Revises: 0004_durable_order_intents
Create Date: 2026-07-12
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_audit_database_guards"
down_revision: str | None = "0004_durable_order_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER prevent_audit_events_update "
            "BEFORE UPDATE ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'audit_events are append-only'); END"
        )
        op.execute(
            "CREATE TRIGGER prevent_audit_events_delete "
            "BEFORE DELETE ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'audit_events are append-only'); END"
        )
    elif bind.dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION reject_audit_event_mutation() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'audit_events are append-only'; RETURN NULL; END; $$"
        )
        op.execute(
            "CREATE TRIGGER prevent_audit_events_mutation "
            "BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_events "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_event_mutation()"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER prevent_audit_events_delete")
        op.execute("DROP TRIGGER prevent_audit_events_update")
    elif bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER prevent_audit_events_mutation ON audit_events")
        op.execute("DROP FUNCTION reject_audit_event_mutation()")
