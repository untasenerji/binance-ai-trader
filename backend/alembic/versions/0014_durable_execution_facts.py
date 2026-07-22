"""Add durable execution facts, source-bound quarantine, and fill guards.

Revision ID: 0014_durable_execution_facts
Revises: 0013_execution_safety_core
Create Date: 2026-07-21
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_durable_execution_facts"
down_revision: str | None = "0013_execution_safety_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MARKERS = "migration_execution_markers"
_FILL_TABLE = "durable_intent_fills"
_JOURNAL_TABLE = "exchange_fill_fact_journal"
_SOURCE_TABLE = "durable_evidence_quarantine_sources"
_SOURCE_RESOLUTION_TABLE = "durable_evidence_quarantine_source_resolutions"
_RECEIPT_TABLE = "durable_adapter_query_receipts"
_ADMISSION_TABLE = "durable_entry_admission_decisions"
_GRANT_TABLE = "durable_entry_authorization_grants"


def _table_exists(bind: sa.Connection, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def _columns(bind: sa.Connection, table_name: str) -> dict[str, dict[str, object]]:
    return {str(column["name"]): column for column in sa.inspect(bind).get_columns(table_name)}


def _marker_completed(bind: sa.Connection, checkpoint: str) -> bool:
    markers = sa.Table(_MARKERS, sa.MetaData(), autoload_with=bind)
    return bool(
        bind.scalar(
            sa.select(sa.func.count())
            .select_from(markers)
            .where(
                markers.c.migration_revision == revision,
                markers.c.checkpoint == checkpoint,
            )
        )
    )


def _mark_completed(bind: sa.Connection, checkpoint: str) -> None:
    if _marker_completed(bind, checkpoint):
        return
    markers = sa.Table(_MARKERS, sa.MetaData(), autoload_with=bind)
    bind.execute(markers.insert().values(migration_revision=revision, checkpoint=checkpoint))


def _run_step(
    bind: sa.Connection,
    checkpoint: str,
    operation: Callable[[], None],
    postcondition: Callable[[], bool],
) -> None:
    if _marker_completed(bind, checkpoint) and postcondition():
        return
    operation()
    if not postcondition():
        raise RuntimeError(f"0014 postcondition failed: {checkpoint}")
    if os.environ.get("UTA_0014_FAIL_AFTER") == checkpoint:
        raise RuntimeError(f"0014 checkpoint failure injected after {checkpoint}")
    _mark_completed(bind, checkpoint)


def _ensure_index(bind: sa.Connection, table_name: str, column_name: str) -> None:
    index_name = f"ix_{table_name}_{column_name}"
    if index_name not in {str(item["name"]) for item in sa.inspect(bind).get_indexes(table_name)}:
        op.create_index(index_name, table_name, [column_name])


def _trigger_handles_operations(definition: object, operations: tuple[str, ...]) -> bool:
    normalized = str(definition).upper()
    return all(operation in normalized for operation in operations)


def _create_execution_tables(bind: sa.Connection) -> None:
    if not _table_exists(bind, _JOURNAL_TABLE):
        op.create_table(
            _JOURNAL_TABLE,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("account_id", sa.String(length=128), nullable=False),
            sa.Column("exchange_trade_id", sa.String(length=128), nullable=False),
            sa.Column("client_order_id", sa.String(length=128), nullable=False),
            sa.Column("intent_id", sa.String(length=128), nullable=True),
            sa.Column("symbol", sa.String(length=32), nullable=False),
            sa.Column("side", sa.String(length=8), nullable=False),
            sa.Column("quantity", sa.String(length=64), nullable=False),
            sa.Column("cumulative_quantity", sa.String(length=64), nullable=False),
            sa.Column("price", sa.String(length=64), nullable=False),
            sa.Column("fee", sa.String(length=64), nullable=False),
            sa.Column("fee_asset", sa.String(length=32), nullable=False),
            sa.Column("exchange_timestamp", sa.DateTime(timezone=True), nullable=False),
            sa.Column("observation_source", sa.String(length=64), nullable=False),
            sa.Column("observation_reference", sa.String(length=128), nullable=False),
            sa.Column("observation_correlation", sa.String(length=128), nullable=True),
            sa.Column("provenance_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("semantic_fingerprint", sa.String(length=64), nullable=False),
            sa.Column(
                "materialize_simulated_protection",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "received_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "apply_status", sa.String(length=32), nullable=False, server_default="PENDING"
            ),
            sa.Column("apply_attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_apply_error", sa.Text(), nullable=True),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "account_id",
                "exchange_trade_id",
                name="uq_exchange_fill_fact_account_trade",
            ),
        )
    for column_name in ("account_id", "client_order_id", "intent_id", "apply_status"):
        _ensure_index(bind, _JOURNAL_TABLE, column_name)

    if not _table_exists(bind, _RECEIPT_TABLE):
        op.create_table(
            _RECEIPT_TABLE,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("receipt_id", sa.String(length=128), nullable=False),
            sa.Column("account_id", sa.String(length=128), nullable=False),
            sa.Column("adapter_instance_id", sa.String(length=128), nullable=False),
            sa.Column("query_id", sa.String(length=128), nullable=False),
            sa.Column("correlation_id", sa.String(length=128), nullable=False),
            sa.Column("query_epoch", sa.Integer(), nullable=False),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("server_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("query_type", sa.String(length=64), nullable=False),
            sa.Column("response_fingerprint", sa.String(length=64), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("receipt_fingerprint", sa.String(length=64), nullable=False),
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
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("receipt_id", name="uq_adapter_receipt_id"),
            sa.UniqueConstraint("account_id", "query_id", name="uq_adapter_receipt_account_query"),
            sa.UniqueConstraint(
                "account_id",
                "adapter_instance_id",
                "query_epoch",
                name="uq_adapter_receipt_adapter_epoch",
            ),
            sa.UniqueConstraint("receipt_fingerprint", name="uq_adapter_receipt_fingerprint"),
        )
    for column_name in ("receipt_id", "account_id", "status"):
        _ensure_index(bind, _RECEIPT_TABLE, column_name)

    if not _table_exists(bind, _ADMISSION_TABLE):
        op.create_table(
            _ADMISSION_TABLE,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("decision_id", sa.String(length=128), nullable=False),
            sa.Column("account_id", sa.String(length=128), nullable=False),
            sa.Column("client_order_id", sa.String(length=128), nullable=False),
            sa.Column("plan_id", sa.String(length=128), nullable=False),
            sa.Column("risk_policy_id", sa.String(length=128), nullable=False),
            sa.Column("risk_policy_version", sa.Integer(), nullable=False),
            sa.Column("risk_policy_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("envelope_version", sa.Integer(), nullable=False),
            sa.Column("envelope_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("symbol", sa.String(length=32), nullable=False),
            sa.Column("side", sa.String(length=16), nullable=False),
            sa.Column("max_quantity", sa.String(length=64), nullable=False),
            sa.Column("max_notional_usdt", sa.String(length=64), nullable=False),
            sa.Column("leverage", sa.Integer(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("grant_generation", sa.Integer(), nullable=False),
            sa.Column("failure_epoch", sa.Integer(), nullable=False),
            sa.Column("recovery_epoch", sa.Integer(), nullable=False),
            sa.Column("query_epoch", sa.Integer(), nullable=False),
            sa.Column("grant_id", sa.String(length=128), nullable=False),
            sa.Column("decision_fingerprint", sa.String(length=64), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("decision_id", name="uq_entry_admission_decision_id"),
            sa.UniqueConstraint("client_order_id", name="uq_entry_admission_client_order"),
            sa.UniqueConstraint("decision_fingerprint", name="uq_entry_admission_fingerprint"),
        )
    for column_name in ("decision_id", "account_id", "client_order_id", "plan_id", "expires_at"):
        _ensure_index(bind, _ADMISSION_TABLE, column_name)


def _execution_tables_complete(bind: sa.Connection) -> bool:
    return all(
        _table_exists(bind, table_name)
        for table_name in (_JOURNAL_TABLE, _RECEIPT_TABLE, _ADMISSION_TABLE)
    )


def _remove_source_append_only_guard(bind: sa.Connection) -> None:
    if bind.dialect.name == "sqlite":
        for operation in ("update", "delete"):
            bind.execute(sa.text(f"DROP TRIGGER IF EXISTS prevent_{_SOURCE_TABLE}_{operation}"))
    elif bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(f"DROP TRIGGER IF EXISTS prevent_{_SOURCE_TABLE}_mutation ON {_SOURCE_TABLE}")
        )


def _restore_source_append_only_guard(bind: sa.Connection) -> None:
    if bind.dialect.name == "sqlite":
        for operation in ("update", "delete"):
            trigger_name = f"prevent_{_SOURCE_TABLE}_{operation}"
            bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name}"))
            bind.execute(
                sa.text(
                    f"CREATE TRIGGER {trigger_name} BEFORE {operation.upper()} ON {_SOURCE_TABLE} "
                    "BEGIN SELECT RAISE(ABORT, 'quarantine sources are append-only'); END"
                )
            )
    elif bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION reject_immutable_evidence_mutation() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'immutable evidence is append-only'; RETURN NULL; END; $$"
            )
        )
        trigger_name = f"prevent_{_SOURCE_TABLE}_mutation"
        bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {_SOURCE_TABLE}"))
        bind.execute(
            sa.text(
                f"CREATE TRIGGER {trigger_name} BEFORE UPDATE OR DELETE OR TRUNCATE "
                f"ON {_SOURCE_TABLE} FOR EACH STATEMENT "
                "EXECUTE FUNCTION reject_immutable_evidence_mutation()"
            )
        )


def _source_append_only_guard_complete(bind: sa.Connection) -> bool:
    if bind.dialect.name == "sqlite":
        names = set(bind.scalars(sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")))
        return {
            f"prevent_{_SOURCE_TABLE}_update",
            f"prevent_{_SOURCE_TABLE}_delete",
        }.issubset(names)
    if bind.dialect.name == "postgresql":
        row = bind.execute(
            sa.text(
                "SELECT triggers.tgenabled, functions.proname, pg_get_triggerdef(triggers.oid) "
                "FROM pg_trigger triggers JOIN pg_class tables ON tables.oid = triggers.tgrelid "
                "JOIN pg_proc functions ON functions.oid = triggers.tgfoid "
                "WHERE tables.relname = :table_name AND triggers.tgname = :trigger_name "
                "AND NOT triggers.tgisinternal"
            ),
            {
                "table_name": _SOURCE_TABLE,
                "trigger_name": f"prevent_{_SOURCE_TABLE}_mutation",
            },
        ).first()
        return bool(
            row is not None
            and row[0] == "O"
            and row[1] == "reject_immutable_evidence_mutation"
            and _trigger_handles_operations(row[2], ("UPDATE", "DELETE", "TRUNCATE"))
        )
    return True


def _add_source_identity_and_resolution(bind: sa.Connection) -> None:
    if not _table_exists(bind, _SOURCE_TABLE):
        raise RuntimeError("0014 requires quarantine source evidence from 0013")
    _remove_source_append_only_guard(bind)
    try:
        existing = _columns(bind, _SOURCE_TABLE)
        if "economic_key" not in existing:
            op.add_column(
                _SOURCE_TABLE, sa.Column("economic_key", sa.String(length=256), nullable=True)
            )
        if "attempt_id" not in existing:
            op.add_column(
                _SOURCE_TABLE, sa.Column("attempt_id", sa.String(length=128), nullable=True)
            )
        source = sa.Table(_SOURCE_TABLE, sa.MetaData(), autoload_with=bind)
        cases = sa.Table("durable_evidence_quarantines", sa.MetaData(), autoload_with=bind)
        bind.execute(
            source.update()
            .where(source.c.economic_key.is_(None))
            .values(
                economic_key=sa.select(cases.c.economic_key)
                .where(cases.c.quarantine_id == source.c.quarantine_id)
                .scalar_subquery()
            )
        )
        bind.execute(
            source.update().where(source.c.attempt_id.is_(None)).values(attempt_id="attempt-1")
        )
        if bind.scalar(
            sa.select(sa.func.count())
            .select_from(source)
            .where(sa.or_(source.c.economic_key.is_(None), source.c.attempt_id.is_(None)))
        ):
            raise RuntimeError("0014 cannot derive complete quarantine source identity")
        current = _columns(bind, _SOURCE_TABLE)
        if bool(current["economic_key"]["nullable"]) or bool(current["attempt_id"]["nullable"]):
            with op.batch_alter_table(_SOURCE_TABLE) as batch:
                batch.alter_column(
                    "economic_key", existing_type=sa.String(length=256), nullable=False
                )
                batch.alter_column(
                    "attempt_id", existing_type=sa.String(length=128), nullable=False
                )
        if not _table_exists(bind, _SOURCE_RESOLUTION_TABLE):
            op.create_table(
                _SOURCE_RESOLUTION_TABLE,
                sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
                sa.Column("resolution_id", sa.String(length=128), nullable=False),
                sa.Column("source_id", sa.Integer(), nullable=False),
                sa.Column("quarantine_id", sa.String(length=128), nullable=False),
                sa.Column("account_id", sa.String(length=128), nullable=False),
                sa.Column("operator_id", sa.String(length=128), nullable=False),
                sa.Column("verified_evidence_source", sa.String(length=64), nullable=False),
                sa.Column("verified_query_reference", sa.String(length=128), nullable=False),
                sa.Column("verified_evidence_fingerprint", sa.String(length=64), nullable=False),
                sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
                sa.Column(
                    "created_at",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.text("CURRENT_TIMESTAMP"),
                ),
                sa.ForeignKeyConstraint(
                    ["source_id"],
                    [f"{_SOURCE_TABLE}.id"],
                    name="fk_quarantine_source_resolution_source",
                ),
                sa.ForeignKeyConstraint(
                    ["quarantine_id"],
                    ["durable_evidence_quarantines.quarantine_id"],
                    name="fk_quarantine_source_resolution_case",
                ),
                sa.PrimaryKeyConstraint("id"),
                sa.UniqueConstraint(
                    "resolution_id", name="uq_evidence_quarantine_source_resolution_id"
                ),
                sa.UniqueConstraint("source_id", name="uq_evidence_quarantine_source_resolution"),
            )
        for column_name in ("source_id", "quarantine_id", "account_id"):
            _ensure_index(bind, _SOURCE_RESOLUTION_TABLE, column_name)
    finally:
        _restore_source_append_only_guard(bind)


def _source_identity_complete(bind: sa.Connection) -> bool:
    if not _table_exists(bind, _SOURCE_TABLE) or not _table_exists(bind, _SOURCE_RESOLUTION_TABLE):
        return False
    columns = _columns(bind, _SOURCE_TABLE)
    return (
        "economic_key" in columns
        and "attempt_id" in columns
        and not bool(columns["economic_key"]["nullable"])
        and not bool(columns["attempt_id"]["nullable"])
        and _source_append_only_guard_complete(bind)
    )


def _add_grant_receipt_columns(bind: sa.Connection) -> None:
    if not _table_exists(bind, _GRANT_TABLE):
        raise RuntimeError("0014 requires durable entry authorization grants")
    existing = _columns(bind, _GRANT_TABLE)
    additions: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
        ("query_receipt_id", sa.String(length=128)),
        ("query_epoch", sa.Integer()),
        ("query_correlation_id", sa.String(length=128)),
        ("query_response_fingerprint", sa.String(length=64)),
    )
    for column_name, column_type in additions:
        if column_name not in existing:
            op.add_column(_GRANT_TABLE, sa.Column(column_name, column_type, nullable=True))
    _ensure_index(bind, _GRANT_TABLE, "query_receipt_id")


def _grant_columns_complete(bind: sa.Connection) -> bool:
    if not _table_exists(bind, _GRANT_TABLE):
        return False
    columns = _columns(bind, _GRANT_TABLE)
    return {
        "query_receipt_id",
        "query_epoch",
        "query_correlation_id",
        "query_response_fingerprint",
    }.issubset(columns)


def _sqlite_fill_guards(bind: sa.Connection) -> None:
    for operation in ("update", "delete"):
        bind.execute(sa.text(f"DROP TRIGGER IF EXISTS prevent_{_FILL_TABLE}_{operation}"))
        bind.execute(
            sa.text(
                f"CREATE TRIGGER prevent_{_FILL_TABLE}_{operation} "
                f"BEFORE {operation.upper()} ON {_FILL_TABLE} BEGIN "
                "SELECT RAISE(ABORT, 'durable fills are append-only'); END"
            )
        )
    bind.execute(sa.text(f"DROP TRIGGER IF EXISTS prevent_{_JOURNAL_TABLE}_economic_update"))
    bind.execute(sa.text(f"DROP TRIGGER IF EXISTS prevent_{_JOURNAL_TABLE}_delete"))
    immutable_columns = (
        "account_id",
        "exchange_trade_id",
        "client_order_id",
        "intent_id",
        "symbol",
        "side",
        "quantity",
        "cumulative_quantity",
        "price",
        "fee",
        "fee_asset",
        "exchange_timestamp",
        "observation_source",
        "observation_reference",
        "observation_correlation",
        "provenance_fingerprint",
        "semantic_fingerprint",
        "materialize_simulated_protection",
        "received_at",
    )
    predicate = " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in immutable_columns)
    bind.execute(
        sa.text(
            f"CREATE TRIGGER prevent_{_JOURNAL_TABLE}_economic_update "
            f"BEFORE UPDATE ON {_JOURNAL_TABLE} WHEN {predicate} BEGIN "
            "SELECT RAISE(ABORT, 'exchange fill facts are immutable'); END"
        )
    )
    bind.execute(
        sa.text(
            f"CREATE TRIGGER prevent_{_JOURNAL_TABLE}_delete "
            f"BEFORE DELETE ON {_JOURNAL_TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'exchange fill facts are append-only'); END"
        )
    )


def _postgresql_fill_guards(bind: sa.Connection) -> None:
    bind.execute(
        sa.text(
            "CREATE OR REPLACE FUNCTION reject_immutable_fill_mutation() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'durable fills are append-only'; RETURN NULL; END; $$"
        )
    )
    bind.execute(
        sa.text(
            "CREATE OR REPLACE FUNCTION reject_exchange_fill_fact_economic_mutation() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN "
            "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION "
            "'exchange fill facts are append-only'; END IF; "
            "IF NEW.account_id IS DISTINCT FROM OLD.account_id "
            "OR NEW.exchange_trade_id IS DISTINCT FROM OLD.exchange_trade_id "
            "OR NEW.client_order_id IS DISTINCT FROM OLD.client_order_id "
            "OR NEW.intent_id IS DISTINCT FROM OLD.intent_id "
            "OR NEW.symbol IS DISTINCT FROM OLD.symbol OR NEW.side IS DISTINCT FROM OLD.side "
            "OR NEW.quantity IS DISTINCT FROM OLD.quantity "
            "OR NEW.cumulative_quantity IS DISTINCT FROM OLD.cumulative_quantity "
            "OR NEW.price IS DISTINCT FROM OLD.price OR NEW.fee IS DISTINCT FROM OLD.fee "
            "OR NEW.fee_asset IS DISTINCT FROM OLD.fee_asset "
            "OR NEW.exchange_timestamp IS DISTINCT FROM OLD.exchange_timestamp "
            "OR NEW.observation_source IS DISTINCT FROM OLD.observation_source "
            "OR NEW.observation_reference IS DISTINCT FROM OLD.observation_reference "
            "OR NEW.observation_correlation IS DISTINCT FROM OLD.observation_correlation "
            "OR NEW.provenance_fingerprint IS DISTINCT FROM OLD.provenance_fingerprint "
            "OR NEW.semantic_fingerprint IS DISTINCT FROM OLD.semantic_fingerprint "
            "OR NEW.materialize_simulated_protection IS DISTINCT "
            "FROM OLD.materialize_simulated_protection "
            "OR NEW.received_at IS DISTINCT FROM OLD.received_at THEN "
            "RAISE EXCEPTION 'exchange fill fact economic mutation denied'; END IF; "
            "RETURN NEW; END; $$"
        )
    )
    expected_guards = (
        (_FILL_TABLE, f"prevent_{_FILL_TABLE}_mutation", "reject_immutable_fill_mutation"),
        (
            _JOURNAL_TABLE,
            f"prevent_{_JOURNAL_TABLE}_mutation",
            "reject_exchange_fill_fact_economic_mutation",
        ),
    )
    preparer = bind.dialect.identifier_preparer
    for table_name, trigger_name, function_name in expected_guards:
        # A damaged migration can leave the expected trigger name attached to a
        # different table. Remove that stale topology before creating the guard
        # on its one allowed table; the catalog names are quoted defensively.
        stray_rows = bind.execute(
            sa.text(
                "SELECT schemas.nspname, tables.relname "
                "FROM pg_trigger triggers "
                "JOIN pg_class tables ON tables.oid = triggers.tgrelid "
                "JOIN pg_namespace schemas ON schemas.oid = tables.relnamespace "
                "WHERE triggers.tgname = :trigger_name "
                "AND NOT triggers.tgisinternal "
                "AND schemas.nspname = current_schema() "
                "AND tables.relname != :expected_table"
            ),
            {"trigger_name": trigger_name, "expected_table": table_name},
        )
        for schema_name, stray_table in stray_rows:
            qualified_table = (
                f"{preparer.quote(str(schema_name))}.{preparer.quote(str(stray_table))}"
            )
            bind.execute(
                sa.text(
                    f"DROP TRIGGER IF EXISTS {preparer.quote(trigger_name)} ON {qualified_table}"
                )
            )
        bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}"))
        bind.execute(
            sa.text(
                f"CREATE TRIGGER {trigger_name} BEFORE UPDATE OR DELETE ON {table_name} "
                f"FOR EACH ROW EXECUTE FUNCTION {function_name}()"
            )
        )


def _install_fill_guards(bind: sa.Connection) -> None:
    if bind.dialect.name == "sqlite":
        _sqlite_fill_guards(bind)
    elif bind.dialect.name == "postgresql":
        _postgresql_fill_guards(bind)


def _fill_guards_complete(bind: sa.Connection) -> bool:
    if not _table_exists(bind, _FILL_TABLE) or not _table_exists(bind, _JOURNAL_TABLE):
        return False
    if bind.dialect.name == "sqlite":
        names = set(bind.scalars(sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")))
        return {
            f"prevent_{_FILL_TABLE}_update",
            f"prevent_{_FILL_TABLE}_delete",
            f"prevent_{_JOURNAL_TABLE}_economic_update",
            f"prevent_{_JOURNAL_TABLE}_delete",
        }.issubset(names)
    if bind.dialect.name == "postgresql":
        expected = (
            (_FILL_TABLE, f"prevent_{_FILL_TABLE}_mutation", "reject_immutable_fill_mutation"),
            (
                _JOURNAL_TABLE,
                f"prevent_{_JOURNAL_TABLE}_mutation",
                "reject_exchange_fill_fact_economic_mutation",
            ),
        )
        for table_name, trigger_name, function_name in expected:
            row = bind.execute(
                sa.text(
                    "SELECT triggers.tgenabled, functions.proname, function_schemas.nspname, "
                    "pg_get_triggerdef(triggers.oid) "
                    "FROM pg_trigger triggers "
                    "JOIN pg_class tables ON tables.oid = triggers.tgrelid "
                    "JOIN pg_proc functions ON functions.oid = triggers.tgfoid "
                    "JOIN pg_namespace table_schemas ON table_schemas.oid = tables.relnamespace "
                    "JOIN pg_namespace function_schemas "
                    "ON function_schemas.oid = functions.pronamespace "
                    "WHERE tables.relname = :table_name AND triggers.tgname = :trigger_name "
                    "AND table_schemas.nspname = current_schema() AND NOT triggers.tgisinternal"
                ),
                {"table_name": table_name, "trigger_name": trigger_name},
            ).first()
            if (
                row is None
                or row[0] != "O"
                or row[1] != function_name
                or row[2] != bind.scalar(sa.text("SELECT current_schema()"))
            ):
                return False
            if not _trigger_handles_operations(row[3], ("UPDATE", "DELETE")):
                return False
        stray_count = bind.scalar(
            sa.text(
                "SELECT COUNT(*) FROM pg_trigger triggers "
                "JOIN pg_class tables ON tables.oid = triggers.tgrelid "
                "JOIN pg_namespace schemas ON schemas.oid = tables.relnamespace "
                "WHERE schemas.nspname = current_schema() AND NOT triggers.tgisinternal "
                "AND triggers.tgname IN (:fill_trigger, :journal_trigger) "
                "AND NOT ((tables.relname = :fill_table AND triggers.tgname = :fill_trigger) "
                "OR (tables.relname = :journal_table AND triggers.tgname = :journal_trigger))"
            ),
            {
                "fill_trigger": f"prevent_{_FILL_TABLE}_mutation",
                "journal_trigger": f"prevent_{_JOURNAL_TABLE}_mutation",
                "fill_table": _FILL_TABLE,
                "journal_table": _JOURNAL_TABLE,
            },
        )
        if stray_count:
            return False
    return True


def _install_execution_evidence_guards(bind: sa.Connection) -> None:
    guarded_tables = (_SOURCE_RESOLUTION_TABLE, _ADMISSION_TABLE)
    if bind.dialect.name == "sqlite":
        for table_name in guarded_tables:
            for operation in ("update", "delete"):
                trigger_name = f"prevent_{table_name}_{operation}"
                bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name}"))
                bind.execute(
                    sa.text(
                        f"CREATE TRIGGER {trigger_name} BEFORE {operation.upper()} ON {table_name} "
                        "BEGIN SELECT RAISE(ABORT, "
                        "'immutable execution evidence is append-only'); END"
                    )
                )
    elif bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION reject_immutable_execution_evidence_mutation() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'immutable execution evidence is append-only'; "
                "RETURN NULL; END; $$"
            )
        )
        for table_name in guarded_tables:
            trigger_name = f"prevent_{table_name}_mutation"
            bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}"))
            bind.execute(
                sa.text(
                    f"CREATE TRIGGER {trigger_name} BEFORE UPDATE OR DELETE ON {table_name} "
                    "FOR EACH ROW EXECUTE FUNCTION "
                    "reject_immutable_execution_evidence_mutation()"
                )
            )


def _execution_evidence_guards_complete(bind: sa.Connection) -> bool:
    guarded_tables = (_SOURCE_RESOLUTION_TABLE, _ADMISSION_TABLE)
    if not all(_table_exists(bind, table_name) for table_name in guarded_tables):
        return False
    if bind.dialect.name == "sqlite":
        names = set(bind.scalars(sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")))
        return all(
            f"prevent_{table_name}_{operation}" in names
            for table_name in guarded_tables
            for operation in ("update", "delete")
        )
    if bind.dialect.name == "postgresql":
        for table_name in guarded_tables:
            trigger_name = f"prevent_{table_name}_mutation"
            row = bind.execute(
                sa.text(
                    "SELECT triggers.tgenabled, functions.proname, pg_get_triggerdef(triggers.oid) "
                    "FROM pg_trigger triggers "
                    "JOIN pg_class tables ON tables.oid = triggers.tgrelid "
                    "JOIN pg_proc functions ON functions.oid = triggers.tgfoid "
                    "WHERE tables.relname = :table_name AND triggers.tgname = :trigger_name "
                    "AND NOT triggers.tgisinternal"
                ),
                {"table_name": table_name, "trigger_name": trigger_name},
            ).first()
            if (
                row is None
                or row[0] != "O"
                or row[1] != "reject_immutable_execution_evidence_mutation"
            ):
                return False
            if not _trigger_handles_operations(row[2], ("UPDATE", "DELETE")):
                return False
    return True


def _verify(bind: sa.Connection) -> None:
    if not _execution_tables_complete(bind):
        raise RuntimeError("0014 execution tables are incomplete")
    if not _source_identity_complete(bind):
        raise RuntimeError("0014 quarantine source identity is incomplete")
    if not _grant_columns_complete(bind):
        raise RuntimeError("0014 grant receipt columns are incomplete")
    if not _fill_guards_complete(bind):
        raise RuntimeError("0014 fill append-only guards are incomplete")
    if not _execution_evidence_guards_complete(bind):
        raise RuntimeError("0014 immutable execution evidence guards are incomplete")
    for checkpoint in (
        "execution_tables",
        "source_identity",
        "grant_receipts",
        "fill_guards",
        "execution_evidence_guards",
    ):
        if not _marker_completed(bind, checkpoint):
            raise RuntimeError(f"0014 migration marker missing: {checkpoint}")


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, _MARKERS):
        raise RuntimeError("0014 requires migration execution markers from 0012")
    _run_step(
        bind,
        "execution_tables",
        lambda: _create_execution_tables(bind),
        lambda: _execution_tables_complete(bind),
    )
    _run_step(
        bind,
        "source_identity",
        lambda: _add_source_identity_and_resolution(bind),
        lambda: _source_identity_complete(bind),
    )
    _run_step(
        bind,
        "grant_receipts",
        lambda: _add_grant_receipt_columns(bind),
        lambda: _grant_columns_complete(bind),
    )
    _run_step(
        bind,
        "fill_guards",
        lambda: _install_fill_guards(bind),
        lambda: _fill_guards_complete(bind),
    )
    _run_step(
        bind,
        "execution_evidence_guards",
        lambda: _install_execution_evidence_guards(bind),
        lambda: _execution_evidence_guards_complete(bind),
    )
    _verify(bind)


def downgrade() -> None:
    raise RuntimeError("0014 durable execution facts migration is forward-only")
