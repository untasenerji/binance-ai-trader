"""Separate rehearsal evidence and harden durable risk/query records.

Revision ID: 0009_evidence_risk_hardening
Revises: 0008_processed_events_temp
Create Date: 2026-07-15
"""

import hashlib
import json
from collections.abc import Mapping, Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_evidence_risk_hardening"
down_revision: str | None = "0008_processed_events_temp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _query_provenance(row: Mapping[str, object]) -> str:
    canonical = json.dumps(
        {
            "attempt_number": int(row["attempt_number"]),
            "client_order_namespace": "NORMAL",
            "economic_key": str(row["economic_key"]),
            "found": bool(row["found"]),
            "observed_at_ms": int(row["observed_at_ms"]),
            "query_client_order_id": str(row["query_client_order_id"]),
            "query_economic_key": str(row["query_economic_key"]),
            "query_reference": str(row["query_reference"]),
            "query_started_at_ms": int(row["query_started_at_ms"]),
            "source": str(row["source"]),
            "stream_watermark_ms": int(row["stream_watermark_ms"]),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _drop_absence_append_only_guard(bind: sa.Connection) -> None:
    table_name = "durable_intent_absence_observations"
    if bind.dialect.name == "sqlite":
        for operation in ("update", "delete"):
            bind.execute(sa.text(f"DROP TRIGGER IF EXISTS prevent_{table_name}_{operation}"))
    elif bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS "
                "prevent_durable_intent_absence_observations_mutation "
                "ON durable_intent_absence_observations"
            )
        )


def _install_absence_append_only_guard(bind: sa.Connection) -> None:
    table_name = "durable_intent_absence_observations"
    if bind.dialect.name == "sqlite":
        for operation in ("update", "delete"):
            bind.execute(
                sa.text(
                    f"CREATE TRIGGER prevent_{table_name}_{operation} "
                    f"BEFORE {operation.upper()} ON {table_name} "
                    f"BEGIN SELECT RAISE(ABORT, '{table_name} are append-only'); END"
                )
            )
    elif bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "CREATE TRIGGER prevent_durable_intent_absence_observations_mutation "
                "BEFORE UPDATE OR DELETE OR TRUNCATE "
                "ON durable_intent_absence_observations FOR EACH STATEMENT "
                "EXECUTE FUNCTION reject_immutable_evidence_mutation()"
            )
        )


def _verify_absence_append_only_guard(bind: sa.Connection) -> None:
    if bind.dialect.name == "sqlite":
        guard_count = bind.scalar(
            sa.text(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
                "AND name IN ('prevent_durable_intent_absence_observations_update', "
                "'prevent_durable_intent_absence_observations_delete')"
            )
        )
        if guard_count != 2:
            raise RuntimeError("absence evidence append-only guards were not restored")
    elif bind.dialect.name == "postgresql":
        guard_count = bind.scalar(
            sa.text(
                "SELECT COUNT(*) FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relname = 'durable_intent_absence_observations' "
                "AND t.tgname = 'prevent_durable_intent_absence_observations_mutation' "
                "AND NOT t.tgisinternal"
            )
        )
        if guard_count != 1:
            raise RuntimeError("absence evidence append-only guard was not restored")


def _normalized_query_values(row: Mapping[str, object]) -> dict[str, object]:
    submitted_at = row["submitted_at_ms"]
    unknown_at = row["unknown_at_ms"]
    observed_at = int(row["observed_at_ms"])
    stream_watermark = int(row["stream_watermark_ms"])
    if submitted_at is None or unknown_at is None:
        raise RuntimeError("legacy absence evidence lacks submission or UNKNOWN time")
    submitted_at = int(submitted_at)
    unknown_at = int(unknown_at)
    if min(submitted_at, unknown_at, observed_at, stream_watermark) < 0:
        raise RuntimeError("legacy absence evidence contains a negative timestamp")
    if submitted_at > unknown_at:
        raise RuntimeError("legacy UNKNOWN time predates submission")

    query_started = row["query_started_at_ms"]
    query_started = unknown_at if query_started is None else int(query_started)
    if query_started < unknown_at or query_started > observed_at:
        raise RuntimeError("legacy query duration is not causally bounded")
    if observed_at < unknown_at or stream_watermark < observed_at:
        raise RuntimeError("legacy observation is not causally after UNKNOWN")

    client_order_id = str(row["client_order_id"])
    economic_key = str(row["economic_key"])
    query_client_order_id = row["query_client_order_id"] or client_order_id
    query_economic_key = row["query_economic_key"] or economic_key
    query_reference = row["query_reference"]
    if not query_reference:
        reference_seed = (
            f"{row['id']}:{client_order_id}:{economic_key}:{row['source']}:{observed_at}"
        )
        digest = hashlib.sha256(reference_seed.encode()).hexdigest()[:32]
        query_reference = f"legacy-query:{row['id']}:{digest}"
    normalized = {
        **dict(row),
        "attempt_number": int(row["attempt_number"]),
        "client_order_namespace": "NORMAL",
        "query_client_order_id": str(query_client_order_id),
        "query_economic_key": str(query_economic_key),
        "query_reference": str(query_reference),
        "query_started_at_ms": query_started,
    }
    normalized["provenance_fingerprint"] = _query_provenance(normalized)
    return normalized


def _make_query_identity_columns_non_nullable(bind: sa.Connection) -> None:
    columns = (
        ("query_reference", sa.String(length=128)),
        ("query_client_order_id", sa.String(length=128)),
        ("query_economic_key", sa.String(length=256)),
        ("query_started_at_ms", sa.Integer()),
    )
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            "durable_intent_absence_observations", recreate="always"
        ) as batch_op:
            for column_name, column_type in columns:
                batch_op.alter_column(
                    column_name,
                    existing_type=column_type,
                    nullable=False,
                )
        return
    for column_name, column_type in columns:
        op.alter_column(
            "durable_intent_absence_observations",
            column_name,
            existing_type=column_type,
            nullable=False,
        )


def _create_policy_version_table() -> None:
    op.create_table(
        "durable_actual_risk_policy_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("worst_stop_exit_price", sa.String(length=64), nullable=False),
        sa.Column("exit_fee_rate", sa.String(length=64), nullable=False),
        sa.Column("funding_buffer_rate", sa.String(length=64), nullable=False),
        sa.Column("funding_interval_count", sa.Integer(), nullable=False),
        sa.Column("risk_budget", sa.String(length=64), nullable=False),
        sa.Column("max_symbol_exposure_usdt", sa.String(length=64), nullable=False),
        sa.Column("max_total_exposure_usdt", sa.String(length=64), nullable=False),
        sa.Column("existing_symbol_exposure_usdt", sa.String(length=64), nullable=False),
        sa.Column("existing_total_exposure_usdt", sa.String(length=64), nullable=False),
        sa.Column("effective_leverage", sa.Integer(), nullable=False),
        sa.Column("required_reserve_usdt", sa.String(length=64), nullable=False),
        sa.Column("effective_equity_usdt", sa.String(length=64), nullable=False),
        sa.Column("protective_stop_reference", sa.String(length=128), nullable=False),
        sa.Column("reduce_only_exit_reference", sa.String(length=128), nullable=False),
        sa.Column("policy_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "version", name="uq_actual_risk_policy_version"),
        sa.UniqueConstraint("policy_fingerprint"),
    )
    op.create_index(
        "ix_durable_actual_risk_policy_versions_plan_id",
        "durable_actual_risk_policy_versions",
        ["plan_id"],
    )


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column(
        "durable_intent_absence_observations",
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "durable_intent_absence_observations",
        sa.Column(
            "client_order_namespace",
            sa.String(length=32),
            nullable=False,
            server_default="NORMAL",
        ),
    )
    op.add_column(
        "durable_intent_absence_observations",
        sa.Column(
            "provenance_fingerprint", sa.String(length=64), nullable=False, server_default=""
        ),
    )
    _drop_absence_append_only_guard(bind)
    try:
        rows = tuple(
            bind.execute(
                sa.text(
                    "SELECT o.id, o.client_order_id, o.economic_key, o.source, "
                    "o.query_reference, o.query_client_order_id, o.query_economic_key, "
                    "o.query_started_at_ms, o.observed_at_ms, o.stream_watermark_ms, "
                    "o.found, i.attempt_number, i.submitted_at_ms, i.unknown_at_ms "
                    "FROM durable_intent_absence_observations o "
                    "JOIN durable_order_intents i "
                    "ON i.client_order_id = o.client_order_id ORDER BY o.id"
                )
            ).mappings()
        )
        for row in rows:
            normalized = _normalized_query_values(row)
            bind.execute(
                sa.text(
                    "UPDATE durable_intent_absence_observations SET "
                    "query_reference = :query_reference, "
                    "query_client_order_id = :query_client_order_id, "
                    "query_economic_key = :query_economic_key, "
                    "query_started_at_ms = :query_started_at_ms, "
                    "attempt_number = :attempt_number, "
                    "client_order_namespace = :client_order_namespace, "
                    "provenance_fingerprint = :provenance_fingerprint WHERE id = :id"
                ),
                normalized,
            )
        _make_query_identity_columns_non_nullable(bind)
        op.create_index(
            "uq_absence_query_reference",
            "durable_intent_absence_observations",
            ["query_reference"],
            unique=True,
        )
        op.create_index(
            "uq_absence_provenance_fingerprint",
            "durable_intent_absence_observations",
            ["provenance_fingerprint"],
            unique=True,
        )
    finally:
        _install_absence_append_only_guard(bind)
        _verify_absence_append_only_guard(bind)

    op.rename_table("durable_plan_protections", "durable_simulated_protections")
    for old_name, new_name in (
        ("protective_stop_reference", "stop_intent_reference"),
        ("reduce_only_exit_reference", "reduce_only_exit_intent_reference"),
        ("confirmed_position_quantity", "protected_position_quantity"),
        ("stop_confirmed", "stop_intent_ready"),
        ("reduce_only_exit_confirmed", "reduce_only_exit_intent_ready"),
    ):
        op.alter_column(
            "durable_simulated_protections",
            old_name,
            new_column_name=new_name,
        )
    op.alter_column(
        "durable_actual_risk_states",
        "confirmed_position_quantity",
        new_column_name="position_quantity",
    )

    _create_policy_version_table()
    op.create_table(
        "durable_risk_reduction_requirements",
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=96), nullable=False),
        sa.Column("required_reduction_quantity", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["durable_actual_risk_policies.plan_id"]),
        sa.PrimaryKeyConstraint("plan_id"),
    )

    immutable_policy_tables = (
        "durable_actual_risk_policies",
        "durable_actual_risk_policy_versions",
    )
    if bind.dialect.name == "sqlite":
        for table_name in immutable_policy_tables:
            for operation in ("update", "delete"):
                bind.execute(
                    sa.text(
                        f"CREATE TRIGGER prevent_{table_name}_{operation} "
                        f"BEFORE {operation.upper()} ON {table_name} "
                        "BEGIN SELECT RAISE(ABORT, 'actual-risk policies are append-only'); END"
                    )
                )
        return
    if bind.dialect.name != "postgresql":
        return
    bind.execute(
        sa.text(
            "ALTER ROLE uta_runtime WITH NOSUPERUSER NOCREATEROLE NOCREATEDB "
            "NOREPLICATION NOBYPASSRLS NOINHERIT"
        )
    )
    bind.execute(
        sa.text(
            "DO $$ BEGIN IF NOT EXISTS "
            "(SELECT 1 FROM pg_roles WHERE rolname = 'uta_policy_config') THEN "
            "CREATE ROLE uta_policy_config NOLOGIN; END IF; END $$"
        )
    )
    bind.execute(
        sa.text(
            "ALTER ROLE uta_policy_config WITH NOLOGIN NOSUPERUSER NOCREATEROLE "
            "NOCREATEDB NOREPLICATION NOBYPASSRLS NOINHERIT"
        )
    )
    bind.execute(
        sa.text(
            "DO $$ BEGIN "
            "EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', "
            "current_database()); "
            "EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM uta_runtime', "
            "current_database()); END $$"
        )
    )
    for table_name in immutable_policy_tables:
        bind.execute(
            sa.text(
                f"CREATE TRIGGER prevent_{table_name}_mutation "
                f"BEFORE UPDATE OR DELETE OR TRUNCATE ON {table_name} "
                "FOR EACH STATEMENT EXECUTE FUNCTION reject_immutable_evidence_mutation()"
            )
        )
        bind.execute(sa.text(f"REVOKE ALL ON TABLE {table_name} FROM uta_runtime"))
        bind.execute(sa.text(f"GRANT SELECT ON TABLE {table_name} TO uta_runtime"))
        bind.execute(sa.text(f"REVOKE ALL ON TABLE {table_name} FROM uta_policy_config"))
        bind.execute(sa.text(f"GRANT SELECT, INSERT ON TABLE {table_name} TO uta_policy_config"))
    for table_name in (
        "durable_simulated_protections",
        "durable_actual_risk_states",
        "durable_risk_reduction_requirements",
    ):
        bind.execute(sa.text(f"REVOKE ALL ON TABLE {table_name} FROM uta_runtime"))
        bind.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table_name} TO uta_runtime"))
    bind.execute(
        sa.text(
            "REVOKE ALL ON SEQUENCE durable_actual_risk_policy_versions_id_seq FROM uta_runtime"
        )
    )
    bind.execute(
        sa.text(
            "GRANT USAGE, SELECT ON SEQUENCE durable_actual_risk_policy_versions_id_seq "
            "TO uta_policy_config"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for table_name in (
            "durable_actual_risk_policies",
            "durable_actual_risk_policy_versions",
        ):
            for operation in ("update", "delete"):
                bind.execute(sa.text(f"DROP TRIGGER IF EXISTS prevent_{table_name}_{operation}"))
    elif bind.dialect.name == "postgresql":
        for table_name in (
            "durable_actual_risk_policies",
            "durable_actual_risk_policy_versions",
        ):
            bind.execute(
                sa.text(f"DROP TRIGGER IF EXISTS prevent_{table_name}_mutation ON {table_name}")
            )
    op.drop_table("durable_risk_reduction_requirements")
    op.drop_index(
        "ix_durable_actual_risk_policy_versions_plan_id",
        table_name="durable_actual_risk_policy_versions",
    )
    op.drop_table("durable_actual_risk_policy_versions")
    op.alter_column(
        "durable_actual_risk_states",
        "position_quantity",
        new_column_name="confirmed_position_quantity",
    )
    for old_name, new_name in (
        ("stop_intent_reference", "protective_stop_reference"),
        ("reduce_only_exit_intent_reference", "reduce_only_exit_reference"),
        ("protected_position_quantity", "confirmed_position_quantity"),
        ("stop_intent_ready", "stop_confirmed"),
        ("reduce_only_exit_intent_ready", "reduce_only_exit_confirmed"),
    ):
        op.alter_column(
            "durable_simulated_protections",
            old_name,
            new_column_name=new_name,
        )
    op.rename_table("durable_simulated_protections", "durable_plan_protections")
    op.drop_index(
        "uq_absence_provenance_fingerprint",
        table_name="durable_intent_absence_observations",
    )
    op.drop_index(
        "uq_absence_query_reference",
        table_name="durable_intent_absence_observations",
    )
    op.drop_column("durable_intent_absence_observations", "provenance_fingerprint")
    op.drop_column("durable_intent_absence_observations", "client_order_namespace")
    op.drop_column("durable_intent_absence_observations", "attempt_number")
