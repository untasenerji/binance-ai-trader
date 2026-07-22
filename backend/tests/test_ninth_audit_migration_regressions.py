"""Real PostgreSQL repair proofs for the forward-only 0014 migration."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.persistence.database import create_database_engine

HEAD = "0014_durable_execution_facts"
PREVIOUS_HEAD = "0013_execution_safety_core"
_FILL_TABLE = "durable_intent_fills"
_JOURNAL_TABLE = "exchange_fill_fact_journal"


def _config(database_url: str) -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture
def ninth_postgresql_database() -> Iterator[str]:
    database_name = f"uta_ninth_migration_{uuid4().hex}"
    database_url = f"postgresql+psycopg://postgres@127.0.0.1:5432/{database_name}"
    admin = create_engine(
        "postgresql+psycopg://postgres@127.0.0.1:5432/postgres",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        yield database_url
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin.dispose()


def _trigger_rows(database_url: str) -> tuple[tuple[str, str, str, str, str], ...]:
    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT triggers.tgname, tables.relname, triggers.tgenabled, "
                    "functions.proname, pg_get_triggerdef(triggers.oid) "
                    "FROM pg_trigger triggers "
                    "JOIN pg_class tables ON tables.oid = triggers.tgrelid "
                    "JOIN pg_proc functions ON functions.oid = triggers.tgfoid "
                    "WHERE triggers.tgname IN "
                    "('prevent_durable_intent_fills_mutation', "
                    "'prevent_exchange_fill_fact_journal_mutation') "
                    "AND NOT triggers.tgisinternal "
                    "ORDER BY triggers.tgname, tables.relname"
                )
            )
            return tuple(cast(tuple[str, str, str, str, str], tuple(row)) for row in rows.tuples())
    finally:
        engine.dispose()


def _assert_fill_guards_repaired(database_url: str) -> None:
    rows = _trigger_rows(database_url)
    expected = {
        ("prevent_durable_intent_fills_mutation", _FILL_TABLE),
        ("prevent_exchange_fill_fact_journal_mutation", _JOURNAL_TABLE),
    }
    assert {(trigger_name, table_name) for trigger_name, table_name, *_ in rows} == expected
    assert len(rows) == len(expected)
    for _trigger_name, _table_name, enabled, _function_name, definition in rows:
        assert enabled == "O"
        assert all(operation in definition.upper() for operation in ("UPDATE", "DELETE"))


def _insert_fill_rows(database_url: str) -> None:
    engine = create_database_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO durable_order_intents "
                    "(account_id, economic_key, attempt_number, client_order_id, plan_id, "
                    "symbol, direction, role, stage_index, quantity, price, "
                    "filled_quantity, status) "
                    "VALUES ('v1-primary', 'ninth-trigger:BTCUSDT:LONG:ENTRY:1', 1, "
                    "'ninth-trigger-entry', 'ninth-trigger-plan', 'BTCUSDT', 'LONG', "
                    "'ENTRY', 1, '0.01', '100', '0', 'NEW')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO durable_intent_fills "
                    "(account_id, client_order_id, trade_id, symbol, side, observation_source, "
                    "observation_reference, semantic_fingerprint, last_quantity, "
                    "cumulative_quantity, fill_price, fee, fee_asset, occurred_at) "
                    "VALUES ('v1-primary', 'ninth-trigger-entry', 'ninth-trigger-fill', "
                    "'BTCUSDT', 'BUY', 'SIMULATED_EXCHANGE', 'ninth-trigger', "
                    ":fingerprint, '0.01', '0.01', '100', '0', 'USDT', CURRENT_TIMESTAMP)"
                ),
                {"fingerprint": "a" * 64},
            )
            connection.execute(
                text(
                    "INSERT INTO exchange_fill_fact_journal "
                    "(account_id, exchange_trade_id, client_order_id, intent_id, symbol, side, "
                    "quantity, cumulative_quantity, price, fee, fee_asset, exchange_timestamp, "
                    "observation_source, observation_reference, provenance_fingerprint, "
                    "semantic_fingerprint, materialize_simulated_protection, apply_status, "
                    "apply_attempt_count) "
                    "VALUES ('v1-primary', 'ninth-trigger-journal', 'ninth-trigger-entry', "
                    "'ninth-trigger-entry', 'BTCUSDT', 'BUY', '0.01', '0.01', '100', '0', "
                    "'USDT', CURRENT_TIMESTAMP, 'SIMULATED_EXCHANGE', 'ninth-trigger', "
                    ":fingerprint, :semantic_fingerprint, false, 'PENDING', 0)"
                ),
                {"fingerprint": "b" * 64, "semantic_fingerprint": "c" * 64},
            )
    finally:
        engine.dispose()


def _assert_actual_mutations_are_denied(database_url: str) -> None:
    engine = create_database_engine(database_url)
    statements = (
        "UPDATE durable_intent_fills SET fee = '1' WHERE trade_id = 'ninth-trigger-fill'",
        "DELETE FROM durable_intent_fills WHERE trade_id = 'ninth-trigger-fill'",
        "UPDATE exchange_fill_fact_journal SET price = '101' "
        "WHERE exchange_trade_id = 'ninth-trigger-journal'",
        "DELETE FROM exchange_fill_fact_journal WHERE exchange_trade_id = 'ninth-trigger-journal'",
    )
    try:
        for statement in statements:
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    with pytest.raises(DBAPIError):
                        connection.execute(text(statement))
                finally:
                    transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.postgresql
@pytest.mark.parametrize(
    "mutation",
    ("missing", "disabled", "wrong_function", "wrong_table", "function_only"),
)
def test_postgresql_0014_repairs_every_fill_trigger_topology(
    ninth_postgresql_database: str,
    mutation: str,
) -> None:
    config = _config(ninth_postgresql_database)
    command.upgrade(config, "head")
    engine = create_database_engine(ninth_postgresql_database)
    try:
        with engine.begin() as connection:
            if mutation == "missing":
                connection.execute(
                    text(
                        "DROP TRIGGER prevent_durable_intent_fills_mutation ON durable_intent_fills"
                    )
                )
            elif mutation == "disabled":
                connection.execute(
                    text(
                        "ALTER TABLE durable_intent_fills DISABLE TRIGGER "
                        "prevent_durable_intent_fills_mutation"
                    )
                )
            elif mutation == "wrong_function":
                connection.execute(
                    text(
                        "CREATE OR REPLACE FUNCTION ninth_wrong_fill_guard() "
                        "RETURNS trigger LANGUAGE plpgsql AS $$ "
                        "BEGIN RETURN NEW; END; $$"
                    )
                )
                connection.execute(
                    text(
                        "DROP TRIGGER prevent_durable_intent_fills_mutation ON durable_intent_fills"
                    )
                )
                connection.execute(
                    text(
                        "CREATE TRIGGER prevent_durable_intent_fills_mutation "
                        "BEFORE UPDATE OR DELETE ON durable_intent_fills "
                        "FOR EACH ROW EXECUTE FUNCTION ninth_wrong_fill_guard()"
                    )
                )
            elif mutation == "wrong_table":
                connection.execute(
                    text(
                        "DROP TRIGGER prevent_durable_intent_fills_mutation ON durable_intent_fills"
                    )
                )
                connection.execute(
                    text(
                        "CREATE TRIGGER prevent_durable_intent_fills_mutation "
                        "BEFORE UPDATE OR DELETE ON exchange_fill_fact_journal "
                        "FOR EACH ROW EXECUTE FUNCTION "
                        "reject_exchange_fill_fact_economic_mutation()"
                    )
                )
            else:
                connection.execute(
                    text(
                        "DROP TRIGGER prevent_durable_intent_fills_mutation ON durable_intent_fills"
                    )
                )
            connection.execute(
                text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": PREVIOUS_HEAD},
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    command.upgrade(config, "head")
    _assert_fill_guards_repaired(ninth_postgresql_database)
    _insert_fill_rows(ninth_postgresql_database)
    _assert_actual_mutations_are_denied(ninth_postgresql_database)


@pytest.mark.postgresql
def test_postgresql_0014_fill_guard_checkpoint_failure_retries_idempotently(
    ninth_postgresql_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(ninth_postgresql_database)
    command.upgrade(config, PREVIOUS_HEAD)
    monkeypatch.setenv("UTA_0014_FAIL_AFTER", "fill_guards")
    with pytest.raises(RuntimeError, match="0014 checkpoint failure"):
        command.upgrade(config, "head")
    monkeypatch.delenv("UTA_0014_FAIL_AFTER")

    command.upgrade(config, "head")
    command.upgrade(config, "head")
    _assert_fill_guards_repaired(ninth_postgresql_database)
