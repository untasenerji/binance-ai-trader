"""Repair PostgreSQL fill-trigger semantic drift with canonical contracts.

Revision ID: 0015_fill_trigger_semantics
Revises: 0014_durable_execution_facts
Create Date: 2026-07-22
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Sequence
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "0015_fill_trigger_semantics"
down_revision: str | None = "0014_durable_execution_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MARKERS = "migration_execution_markers"
_FILL_TABLE = "durable_intent_fills"
_JOURNAL_TABLE = "exchange_fill_fact_journal"
_TRIGGER_TYPE_ROW = 1
_TRIGGER_TYPE_BEFORE = 2
_TRIGGER_TYPE_DELETE = 8
_TRIGGER_TYPE_UPDATE = 16
_EXPECTED_TRIGGER_TYPE = (
    _TRIGGER_TYPE_ROW | _TRIGGER_TYPE_BEFORE | _TRIGGER_TYPE_DELETE | _TRIGGER_TYPE_UPDATE
)


class _FunctionSpec:
    __slots__ = ("table_name", "trigger_name", "function_name", "body")

    def __init__(self, table_name: str, trigger_name: str, function_name: str, body: str) -> None:
        self.table_name = table_name
        self.trigger_name = trigger_name
        self.function_name = function_name
        self.body = body


_FUNCTION_SPECS = (
    _FunctionSpec(
        table_name=_FILL_TABLE,
        trigger_name=f"prevent_{_FILL_TABLE}_mutation",
        function_name="reject_immutable_fill_mutation",
        body="""
        BEGIN
            RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'durable fills are append-only';
            RETURN NULL;
        END;
        """,
    ),
    _FunctionSpec(
        table_name=_JOURNAL_TABLE,
        trigger_name=f"prevent_{_JOURNAL_TABLE}_mutation",
        function_name="reject_exchange_fill_fact_economic_mutation",
        body="""
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'exchange fill facts are append-only';
            END IF;
            IF NEW.account_id IS DISTINCT FROM OLD.account_id
                OR NEW.exchange_trade_id IS DISTINCT FROM OLD.exchange_trade_id
                OR NEW.client_order_id IS DISTINCT FROM OLD.client_order_id
                OR NEW.intent_id IS DISTINCT FROM OLD.intent_id
                OR NEW.symbol IS DISTINCT FROM OLD.symbol
                OR NEW.side IS DISTINCT FROM OLD.side
                OR NEW.quantity IS DISTINCT FROM OLD.quantity
                OR NEW.cumulative_quantity IS DISTINCT FROM OLD.cumulative_quantity
                OR NEW.price IS DISTINCT FROM OLD.price
                OR NEW.fee IS DISTINCT FROM OLD.fee
                OR NEW.fee_asset IS DISTINCT FROM OLD.fee_asset
                OR NEW.exchange_timestamp IS DISTINCT FROM OLD.exchange_timestamp
                OR NEW.observation_source IS DISTINCT FROM OLD.observation_source
                OR NEW.observation_reference IS DISTINCT FROM OLD.observation_reference
                OR NEW.observation_correlation IS DISTINCT FROM OLD.observation_correlation
                OR NEW.provenance_fingerprint IS DISTINCT FROM OLD.provenance_fingerprint
                OR NEW.semantic_fingerprint IS DISTINCT FROM OLD.semantic_fingerprint
                OR NEW.materialize_simulated_protection IS DISTINCT
                    FROM OLD.materialize_simulated_protection
                OR NEW.received_at IS DISTINCT FROM OLD.received_at THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'exchange fill fact economic mutation denied';
            END IF;
            RETURN NEW;
        END;
        """,
    ),
)


def _normalize_sql(value: object) -> str:
    collapsed = re.sub(r"\s+", " ", str(value).strip()).casefold()
    return re.sub(r"\s*([(),;=])\s*", r"\1", collapsed)


def _canonical_body(spec: _FunctionSpec) -> str:
    return _normalize_sql(spec.body)


def _function_body_from_definition(definition: object) -> str | None:
    match = re.search(
        r"\bAS\s+(?P<tag>\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$)"
        r"(?P<body>.*?)(?P=tag)",
        str(definition),
        flags=re.DOTALL | re.IGNORECASE,
    )
    return None if match is None else _normalize_sql(match.group("body"))


def _fingerprint(contract: dict[str, object]) -> str:
    payload = json.dumps(contract, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _expected_function_fingerprint(schema_name: str, spec: _FunctionSpec) -> str:
    return _fingerprint(
        {
            "body": _canonical_body(spec),
            "language": "plpgsql",
            "name": spec.function_name,
            "proconfig": ["search_path=pg_catalog"],
            "schema": schema_name,
            "security_definer": False,
            "returns": "trigger",
        }
    )


def _function_contract(bind: sa.Connection, spec: _FunctionSpec) -> dict[str, object] | None:
    row = (
        bind.execute(
            sa.text(
                "SELECT functions.oid, schemas.nspname AS schema_name, functions.proname, "
                "functions.prosecdef, functions.proconfig, languages.lanname, "
                "pg_get_function_result(functions.oid) AS result_type, "
                "pg_get_functiondef(functions.oid) AS definition "
                "FROM pg_proc functions "
                "JOIN pg_namespace schemas ON schemas.oid = functions.pronamespace "
                "JOIN pg_language languages ON languages.oid = functions.prolang "
                "WHERE functions.proname = :function_name "
                "AND functions.pronargs = 0 "
                "AND schemas.nspname = current_schema()"
            ),
            {"function_name": spec.function_name},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    proconfig = row["proconfig"] or ()
    return {
        "body": _function_body_from_definition(row["definition"]),
        "definition": str(row["definition"]),
        "language": str(row["lanname"]).casefold(),
        "name": str(row["proname"]),
        "oid": int(row["oid"]),
        "proconfig": sorted(str(value) for value in proconfig),
        "schema": str(row["schema_name"]),
        "security_definer": bool(row["prosecdef"]),
        "returns": str(row["result_type"]).casefold(),
    }


def _actual_function_fingerprint(contract: dict[str, object]) -> str:
    return _fingerprint(
        {
            "body": contract["body"],
            "language": contract["language"],
            "name": contract["name"],
            "proconfig": contract["proconfig"],
            "schema": contract["schema"],
            "security_definer": contract["security_definer"],
            "returns": contract["returns"],
        }
    )


def _function_contract_complete(bind: sa.Connection, spec: _FunctionSpec) -> bool:
    contract = _function_contract(bind, spec)
    if contract is None:
        return False
    return _actual_function_fingerprint(contract) == _expected_function_fingerprint(
        str(bind.scalar(sa.text("SELECT current_schema()"))), spec
    )


def _all_function_contracts_complete(bind: sa.Connection) -> bool:
    return all(_function_contract_complete(bind, spec) for spec in _FUNCTION_SPECS)


def _qualified(bind: sa.Connection, schema_name: str, object_name: str) -> str:
    preparer = bind.dialect.identifier_preparer
    return f"{preparer.quote(schema_name)}.{preparer.quote(object_name)}"


def _create_canonical_function(bind: sa.Connection, spec: _FunctionSpec) -> None:
    schema_name = str(bind.scalar(sa.text("SELECT current_schema()")))
    qualified_function = _qualified(bind, schema_name, spec.function_name)
    contract = _function_contract(bind, spec)
    if contract is not None and contract["returns"] != "trigger":
        bind.execute(sa.text(f"DROP FUNCTION {qualified_function}()"))
    bind.execute(
        sa.text(
            f"CREATE OR REPLACE FUNCTION {qualified_function}() "
            "RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER "
            "SET search_path = pg_catalog AS $function$\n"
            f"{spec.body.strip()}\n"
            "$function$"
        )
    )
    bind.execute(sa.text(f"ALTER FUNCTION {qualified_function}() RESET ALL"))
    bind.execute(sa.text(f"ALTER FUNCTION {qualified_function}() SET search_path = pg_catalog"))


def _repair_functions(bind: sa.Connection) -> None:
    for spec in _FUNCTION_SPECS:
        if not _function_contract_complete(bind, spec):
            _create_canonical_function(bind, spec)


def _trigger_contract(bind: sa.Connection, spec: _FunctionSpec) -> dict[str, object] | None:
    row = (
        bind.execute(
            sa.text(
                "SELECT triggers.oid, triggers.tgenabled, triggers.tgtype, triggers.tgnargs, "
                "triggers.tgfoid, tables.relname AS table_name, "
                "table_schemas.nspname AS table_schema, functions.proname AS function_name, "
                "function_schemas.nspname AS function_schema, "
                "pg_get_triggerdef(triggers.oid) AS definition "
                "FROM pg_trigger triggers "
                "JOIN pg_class tables ON tables.oid = triggers.tgrelid "
                "JOIN pg_namespace table_schemas ON table_schemas.oid = tables.relnamespace "
                "JOIN pg_proc functions ON functions.oid = triggers.tgfoid "
                "JOIN pg_namespace function_schemas "
                "ON function_schemas.oid = functions.pronamespace "
                "WHERE triggers.tgname = :trigger_name "
                "AND tables.relname = :table_name "
                "AND table_schemas.nspname = current_schema() "
                "AND NOT triggers.tgisinternal"
            ),
            {"table_name": spec.table_name, "trigger_name": spec.trigger_name},
        )
        .mappings()
        .first()
    )
    return None if row is None else dict(row)


def _trigger_contract_complete(bind: sa.Connection, spec: _FunctionSpec) -> bool:
    function_contract = _function_contract(bind, spec)
    trigger_contract = _trigger_contract(bind, spec)
    if function_contract is None or trigger_contract is None:
        return False
    schema_name = str(bind.scalar(sa.text("SELECT current_schema()")))
    definition = _normalize_sql(trigger_contract["definition"])
    return bool(
        trigger_contract["tgenabled"] == "O"
        and int(trigger_contract["tgtype"]) == _EXPECTED_TRIGGER_TYPE
        and int(trigger_contract["tgnargs"]) == 0
        and int(trigger_contract["tgfoid"]) == int(function_contract["oid"])
        and trigger_contract["table_name"] == spec.table_name
        and trigger_contract["table_schema"] == schema_name
        and trigger_contract["function_name"] == spec.function_name
        and trigger_contract["function_schema"] == schema_name
        and "before delete or update" in definition
        and "for each row" in definition
    )


def _remove_stray_trigger_bindings(bind: sa.Connection, spec: _FunctionSpec) -> None:
    schema_name = str(bind.scalar(sa.text("SELECT current_schema()")))
    rows = bind.execute(
        sa.text(
            "SELECT tables.relname AS table_name "
            "FROM pg_trigger triggers "
            "JOIN pg_class tables ON tables.oid = triggers.tgrelid "
            "JOIN pg_namespace schemas ON schemas.oid = tables.relnamespace "
            "WHERE triggers.tgname = :trigger_name "
            "AND schemas.nspname = :schema_name "
            "AND tables.relname != :expected_table "
            "AND NOT triggers.tgisinternal"
        ),
        {
            "trigger_name": spec.trigger_name,
            "schema_name": schema_name,
            "expected_table": spec.table_name,
        },
    )
    for row in rows.mappings():
        quoted_trigger = bind.dialect.identifier_preparer.quote(spec.trigger_name)
        bind.execute(
            sa.text(
                f"DROP TRIGGER IF EXISTS {quoted_trigger} "
                f"ON {_qualified(bind, schema_name, str(row['table_name']))}"
            )
        )


def _repair_trigger(bind: sa.Connection, spec: _FunctionSpec) -> None:
    schema_name = str(bind.scalar(sa.text("SELECT current_schema()")))
    preparer = bind.dialect.identifier_preparer
    _remove_stray_trigger_bindings(bind, spec)
    qualified_table = _qualified(bind, schema_name, spec.table_name)
    qualified_function = _qualified(bind, schema_name, spec.function_name)
    bind.execute(
        sa.text(f"DROP TRIGGER IF EXISTS {preparer.quote(spec.trigger_name)} ON {qualified_table}")
    )
    bind.execute(
        sa.text(
            f"CREATE TRIGGER {preparer.quote(spec.trigger_name)} "
            f"BEFORE UPDATE OR DELETE ON {qualified_table} "
            f"FOR EACH ROW EXECUTE FUNCTION {qualified_function}()"
        )
    )


def _repair_triggers(bind: sa.Connection) -> None:
    for spec in _FUNCTION_SPECS:
        if not _trigger_contract_complete(bind, spec):
            _repair_trigger(bind, spec)


def _assert_mutation_rejected(
    bind: sa.Connection, statement: str, parameters: dict[str, object]
) -> None:
    try:
        with bind.begin_nested():
            bind.execute(sa.text(statement), parameters)
    except sa.exc.DBAPIError:
        return
    raise RuntimeError("0015 fill trigger behavioral postcondition allowed a forbidden mutation")


def _fill_guard_behavior_complete(bind: sa.Connection) -> bool:
    token = uuid4().hex
    values = {
        "account_id": f"0015-trigger-probe-{token}",
        "client_order_id": f"0015-entry-{token}",
        "economic_key": f"0015:BTCUSDT:LONG:ENTRY:{token}",
        "fill_trade_id": f"0015-fill-{token}",
        "journal_trade_id": f"0015-journal-{token}",
        "semantic_fingerprint": hashlib.sha256(token.encode("utf-8")).hexdigest(),
    }
    probe = bind.begin_nested()
    try:
        bind.execute(
            sa.text(
                "INSERT INTO durable_order_intents "
                "(account_id, economic_key, attempt_number, client_order_id, plan_id, "
                "symbol, direction, role, stage_index, quantity, price, filled_quantity, status) "
                "VALUES (:account_id, :economic_key, 1, :client_order_id, '0015-trigger-plan', "
                "'BTCUSDT', 'LONG', 'ENTRY', 1, '0.01', '100', '0', 'NEW')"
            ),
            values,
        )
        bind.execute(
            sa.text(
                "INSERT INTO durable_intent_fills "
                "(account_id, client_order_id, trade_id, symbol, side, observation_source, "
                "observation_reference, semantic_fingerprint, last_quantity, "
                "cumulative_quantity, fill_price, fee, fee_asset, occurred_at) "
                "VALUES (:account_id, :client_order_id, :fill_trade_id, 'BTCUSDT', 'BUY', "
                "'MIGRATION_PROBE', '0015-trigger-probe', :semantic_fingerprint, "
                "'0.01', '0.01', '100', '0', 'USDT', CURRENT_TIMESTAMP)"
            ),
            values,
        )
        bind.execute(
            sa.text(
                "INSERT INTO exchange_fill_fact_journal "
                "(account_id, exchange_trade_id, client_order_id, intent_id, symbol, side, "
                "quantity, cumulative_quantity, price, fee, fee_asset, exchange_timestamp, "
                "observation_source, observation_reference, provenance_fingerprint, "
                "semantic_fingerprint, materialize_simulated_protection, apply_status, "
                "apply_attempt_count) "
                "VALUES (:account_id, :journal_trade_id, :client_order_id, NULL, 'BTCUSDT', "
                "'BUY', '0.01', '0.01', '100', '0', 'USDT', CURRENT_TIMESTAMP, "
                "'MIGRATION_PROBE', '0015-trigger-probe', :semantic_fingerprint, "
                ":semantic_fingerprint, false, 'PENDING', 0)"
            ),
            values,
        )
        if (
            bind.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM durable_intent_fills WHERE trade_id = :fill_trade_id"
                ),
                values,
            )
            != 1
        ):
            return False
        if (
            bind.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM exchange_fill_fact_journal "
                    "WHERE exchange_trade_id = :journal_trade_id"
                ),
                values,
            )
            != 1
        ):
            return False
        _assert_mutation_rejected(
            bind,
            "UPDATE durable_intent_fills SET fee = '1' WHERE trade_id = :fill_trade_id",
            values,
        )
        _assert_mutation_rejected(
            bind,
            "DELETE FROM durable_intent_fills WHERE trade_id = :fill_trade_id",
            values,
        )
        _assert_mutation_rejected(
            bind,
            "UPDATE exchange_fill_fact_journal SET price = '101' "
            "WHERE exchange_trade_id = :journal_trade_id",
            values,
        )
        _assert_mutation_rejected(
            bind,
            "DELETE FROM exchange_fill_fact_journal WHERE exchange_trade_id = :journal_trade_id",
            values,
        )
        return True
    except (sa.exc.DBAPIError, RuntimeError):
        return False
    finally:
        probe.rollback()
        residual_count = bind.scalar(
            sa.text("SELECT COUNT(*) FROM durable_intent_fills WHERE trade_id = :fill_trade_id"),
            values,
        )
        if residual_count:
            raise RuntimeError("0015 fill trigger probe left test data after rollback")


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
        raise RuntimeError(f"0015 postcondition failed: {checkpoint}")
    if os.environ.get("UTA_0015_FAIL_AFTER") == checkpoint:
        raise RuntimeError(f"0015 checkpoint failure injected after {checkpoint}")
    if os.environ.get("UTA_0015_FAIL_BEFORE_MARKER") == checkpoint:
        raise RuntimeError(f"0015 checkpoint failure injected before marker: {checkpoint}")
    _mark_completed(bind, checkpoint)


def _verify(bind: sa.Connection) -> None:
    checkpoints = ("canonical_functions", "trigger_bindings", "behavioral_probe")
    if not all(_marker_completed(bind, checkpoint) for checkpoint in checkpoints):
        raise RuntimeError("0015 migration markers are incomplete")
    if not _all_function_contracts_complete(bind):
        raise RuntimeError("0015 canonical fill function contract is incomplete")
    if not all(_trigger_contract_complete(bind, spec) for spec in _FUNCTION_SPECS):
        raise RuntimeError("0015 fill trigger contract is incomplete")
    if not _fill_guard_behavior_complete(bind):
        raise RuntimeError("0015 fill trigger behavioral contract is incomplete")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _run_step(
        bind,
        "canonical_functions",
        lambda: _repair_functions(bind),
        lambda: _all_function_contracts_complete(bind),
    )
    _run_step(
        bind,
        "trigger_bindings",
        lambda: _repair_triggers(bind),
        lambda: all(_trigger_contract_complete(bind, spec) for spec in _FUNCTION_SPECS),
    )
    _run_step(
        bind,
        "behavioral_probe",
        lambda: None,
        lambda: _fill_guard_behavior_complete(bind),
    )
    _verify(bind)


def downgrade() -> None:
    raise RuntimeError("0015 is forward-only and cannot be downgraded safely")
