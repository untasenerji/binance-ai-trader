"""Protect processed-event idempotency claims and revoke runtime TEMP access."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_processed_events_temp"
down_revision: str | None = "0007_durable_risk_causality"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for operation in ("update", "delete"):
            bind.execute(
                sa.text(
                    f"CREATE TRIGGER prevent_processed_events_{operation} "
                    f"BEFORE {operation.upper()} ON processed_events "
                    "BEGIN SELECT RAISE(ABORT, 'processed_events are append-only'); END"
                )
            )
        return
    if bind.dialect.name != "postgresql":
        return
    bind.execute(
        sa.text(
            "CREATE TRIGGER prevent_processed_events_mutation "
            "BEFORE UPDATE OR DELETE OR TRUNCATE ON processed_events "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_immutable_evidence_mutation()"
        )
    )
    bind.execute(
        sa.text(
            "DO $$ BEGIN "
            "EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database()); "
            "EXECUTE format("
            "'REVOKE TEMPORARY ON DATABASE %I FROM uta_runtime', current_database()); "
            "END $$"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for operation in ("update", "delete"):
            bind.execute(sa.text(f"DROP TRIGGER IF EXISTS prevent_processed_events_{operation}"))
        return
    if bind.dialect.name != "postgresql":
        return
    bind.execute(
        sa.text("DROP TRIGGER IF EXISTS prevent_processed_events_mutation ON processed_events")
    )
    bind.execute(
        sa.text(
            "DO $$ BEGIN "
            "EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO PUBLIC', current_database()); "
            "END $$"
        )
    )
