"""PostgreSQL regressions for the ninth trigger-semantics follow-up."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.persistence.database import create_database_engine

HEAD = "0015_fill_trigger_semantics"
PREVIOUS_HEAD = "0014_durable_execution_facts"
_FILL_TABLE = "durable_intent_fills"
_JOURNAL_TABLE = "exchange_fill_fact_journal"
_FILL_TRIGGER = f"prevent_{_FILL_TABLE}_mutation"
_JOURNAL_TRIGGER = f"prevent_{_JOURNAL_TABLE}_mutation"
_EXPECTED_TRIGGER_TYPE = 27


def _config(database_url: str) -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _upgrade(database_url: str, revision: str = HEAD) -> None:
    command.upgrade(_config(database_url), revision)


@pytest.fixture
def followup_postgresql_database() -> Iterator[str]:
    database_name = f"uta_ninth_trigger_followup_{uuid4().hex}"
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


def _set_alembic_version(database_url: str, revision: str) -> None:
    engine = create_database_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": revision},
            )
    finally:
        engine.dispose()


def _insert_guarded_rows(database_url: str) -> dict[str, str]:
    token = uuid4().hex
    values = {
        "account_id": f"ninth-trigger-{token}",
        "client_order_id": f"ninth-entry-{token}",
        "economic_key": f"ninth:BTCUSDT:LONG:ENTRY:{token}",
        "fill_trade_id": f"ninth-fill-{token}",
        "journal_trade_id": f"ninth-journal-{token}",
        "fingerprint": "a" * 64,
    }
    engine = create_database_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO durable_order_intents "
                    "(account_id, economic_key, attempt_number, client_order_id, plan_id, "
                    "symbol, direction, role, stage_index, quantity, price, filled_quantity, "
                    "status) VALUES (:account_id, :economic_key, 1, :client_order_id, "
                    "'ninth-trigger-plan', 'BTCUSDT', 'LONG', 'ENTRY', 1, '0.01', '100', '0', "
                    "'NEW')"
                ),
                values,
            )
            connection.execute(
                text(
                    "INSERT INTO durable_intent_fills "
                    "(account_id, client_order_id, trade_id, symbol, side, observation_source, "
                    "observation_reference, semantic_fingerprint, last_quantity, "
                    "cumulative_quantity, fill_price, fee, fee_asset, occurred_at) "
                    "VALUES (:account_id, :client_order_id, :fill_trade_id, 'BTCUSDT', 'BUY', "
                    "'SIMULATED_EXCHANGE', 'ninth-trigger', :fingerprint, '0.01', '0.01', "
                    "'100', '0', 'USDT', CURRENT_TIMESTAMP)"
                ),
                values,
            )
            connection.execute(
                text(
                    "INSERT INTO exchange_fill_fact_journal "
                    "(account_id, exchange_trade_id, client_order_id, intent_id, symbol, side, "
                    "quantity, cumulative_quantity, price, fee, fee_asset, exchange_timestamp, "
                    "observation_source, observation_reference, provenance_fingerprint, "
                    "semantic_fingerprint, materialize_simulated_protection, apply_status, "
                    "apply_attempt_count) VALUES (:account_id, :journal_trade_id, "
                    ":client_order_id, NULL, 'BTCUSDT', 'BUY', '0.01', '0.01', '100', '0', "
                    "'USDT', CURRENT_TIMESTAMP, 'SIMULATED_EXCHANGE', 'ninth-trigger', "
                    ":fingerprint, :fingerprint, false, 'PENDING', 0)"
                ),
                values,
            )
    finally:
        engine.dispose()
    return values


def _assert_mutations_rejected(database_url: str, values: dict[str, str]) -> None:
    statements = (
        (
            "UPDATE durable_intent_fills SET fee = '1' WHERE trade_id = :fill_trade_id",
            values,
        ),
        ("DELETE FROM durable_intent_fills WHERE trade_id = :fill_trade_id", values),
        (
            "UPDATE exchange_fill_fact_journal SET price = '101' "
            "WHERE exchange_trade_id = :journal_trade_id",
            values,
        ),
        (
            "DELETE FROM exchange_fill_fact_journal WHERE exchange_trade_id = :journal_trade_id",
            values,
        ),
    )
    engine = create_database_engine(database_url)
    try:
        for statement, parameters in statements:
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    with pytest.raises(DBAPIError):
                        connection.execute(text(statement), parameters)
                finally:
                    transaction.rollback()
    finally:
        engine.dispose()


def _assert_apply_state_updates_remain_available(database_url: str, values: dict[str, str]) -> None:
    engine = create_database_engine(database_url)
    try:
        with engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE exchange_fill_fact_journal "
                    "SET apply_status = 'APPLYING', apply_attempt_count = 1 "
                    "WHERE exchange_trade_id = :journal_trade_id"
                ),
                values,
            )
            assert result.rowcount == 1
    finally:
        engine.dispose()


def _assert_catalog_contracts(database_url: str) -> None:
    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            schema_name = connection.scalar(text("SELECT current_schema()"))
            for function_name, expected_message in (
                ("reject_immutable_fill_mutation", "durable fills are append-only"),
                (
                    "reject_exchange_fill_fact_economic_mutation",
                    "exchange fill facts are append-only",
                ),
            ):
                function = connection.execute(
                    text(
                        "SELECT schemas.nspname, functions.prosecdef, functions.proconfig, "
                        "languages.lanname, pg_get_function_result(functions.oid), "
                        "pg_get_functiondef(functions.oid) "
                        "FROM pg_proc functions "
                        "JOIN pg_namespace schemas ON schemas.oid = functions.pronamespace "
                        "JOIN pg_language languages ON languages.oid = functions.prolang "
                        "WHERE functions.proname = :function_name "
                        "AND functions.pronargs = 0 "
                        "AND schemas.nspname = current_schema()"
                    ),
                    {"function_name": function_name},
                ).one()
                assert function[0] == schema_name
                assert function[1] is False
                assert list(function[2] or ()) == ["search_path=pg_catalog"]
                assert function[3] == "plpgsql"
                assert function[4] == "trigger"
                assert expected_message in function[5]

            for table_name, trigger_name, function_name in (
                (_FILL_TABLE, _FILL_TRIGGER, "reject_immutable_fill_mutation"),
                (
                    _JOURNAL_TABLE,
                    _JOURNAL_TRIGGER,
                    "reject_exchange_fill_fact_economic_mutation",
                ),
            ):
                trigger = connection.execute(
                    text(
                        "SELECT triggers.tgenabled, triggers.tgtype, triggers.tgnargs, "
                        "tables.relname, table_schemas.nspname, functions.proname, "
                        "function_schemas.nspname, pg_get_triggerdef(triggers.oid) "
                        "FROM pg_trigger triggers "
                        "JOIN pg_class tables ON tables.oid = triggers.tgrelid "
                        "JOIN pg_namespace table_schemas "
                        "ON table_schemas.oid = tables.relnamespace "
                        "JOIN pg_proc functions ON functions.oid = triggers.tgfoid "
                        "JOIN pg_namespace function_schemas "
                        "ON function_schemas.oid = functions.pronamespace "
                        "WHERE triggers.tgname = :trigger_name "
                        "AND tables.relname = :table_name "
                        "AND table_schemas.nspname = current_schema() "
                        "AND NOT triggers.tgisinternal"
                    ),
                    {"table_name": table_name, "trigger_name": trigger_name},
                ).one()
                assert trigger[0] == "O"
                assert trigger[1] == _EXPECTED_TRIGGER_TYPE
                assert trigger[2] == 0
                assert trigger[3] == table_name
                assert trigger[4] == schema_name
                assert trigger[5] == function_name
                assert trigger[6] == schema_name
                definition = trigger[7].casefold()
                assert "before" in definition
                assert "delete" in definition
                assert "update" in definition
                assert "for each row" in definition
    finally:
        engine.dispose()


def _assert_migration_probe_rolled_back(database_url: str) -> None:
    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM durable_intent_fills "
                        "WHERE account_id LIKE '0015-trigger-probe-%'"
                    )
                )
                == 0
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM exchange_fill_fact_journal "
                        "WHERE account_id LIKE '0015-trigger-probe-%'"
                    )
                )
                == 0
            )
    finally:
        engine.dispose()


def _damage_fill_guard(database_url: str, damage: str) -> None:
    engine = create_database_engine(database_url)
    try:
        with engine.begin() as connection:
            if damage == "ineffective_body":
                connection.execute(
                    text(
                        "CREATE OR REPLACE FUNCTION reject_immutable_fill_mutation() "
                        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                        "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'delete only'; END IF; "
                        "RETURN NEW; END; $$"
                    )
                )
            elif damage == "return_new_body":
                connection.execute(
                    text(
                        "CREATE OR REPLACE FUNCTION reject_immutable_fill_mutation() "
                        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$"
                    )
                )
            elif damage == "missing_function":
                connection.execute(text("DROP FUNCTION reject_immutable_fill_mutation() CASCADE"))
            elif damage == "wrong_function":
                connection.execute(
                    text(
                        "CREATE FUNCTION ninth_wrong_fill_guard() RETURNS trigger "
                        "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$"
                    )
                )
                connection.execute(text(f"DROP TRIGGER {_FILL_TRIGGER} ON {_FILL_TABLE}"))
                connection.execute(
                    text(
                        f"CREATE TRIGGER {_FILL_TRIGGER} BEFORE UPDATE OR DELETE ON {_FILL_TABLE} "
                        "FOR EACH ROW EXECUTE FUNCTION ninth_wrong_fill_guard()"
                    )
                )
            elif damage == "wrong_schema":
                connection.execute(text("CREATE SCHEMA ninth_trigger_wrong_schema"))
                connection.execute(
                    text(
                        "CREATE FUNCTION ninth_trigger_wrong_schema."
                        "reject_immutable_fill_mutation() RETURNS trigger LANGUAGE plpgsql "
                        "AS $$ BEGIN RETURN NEW; END; $$"
                    )
                )
                connection.execute(text(f"DROP TRIGGER {_FILL_TRIGGER} ON {_FILL_TABLE}"))
                connection.execute(
                    text(
                        f"CREATE TRIGGER {_FILL_TRIGGER} BEFORE UPDATE OR DELETE ON {_FILL_TABLE} "
                        "FOR EACH ROW EXECUTE FUNCTION "
                        "ninth_trigger_wrong_schema.reject_immutable_fill_mutation()"
                    )
                )
            elif damage == "wrong_table":
                connection.execute(text("CREATE TABLE ninth_trigger_decoy (id integer)"))
                connection.execute(text(f"DROP TRIGGER {_FILL_TRIGGER} ON {_FILL_TABLE}"))
                connection.execute(
                    text(
                        f"CREATE TRIGGER {_FILL_TRIGGER} BEFORE UPDATE OR DELETE "
                        "ON ninth_trigger_decoy FOR EACH ROW "
                        "EXECUTE FUNCTION reject_immutable_fill_mutation()"
                    )
                )
            elif damage == "disabled_trigger":
                connection.execute(
                    text(f"ALTER TABLE {_FILL_TABLE} DISABLE TRIGGER {_FILL_TRIGGER}")
                )
            elif damage in {"update_only", "delete_only"}:
                operation = "UPDATE" if damage == "update_only" else "DELETE"
                connection.execute(text(f"DROP TRIGGER {_FILL_TRIGGER} ON {_FILL_TABLE}"))
                connection.execute(
                    text(
                        f"CREATE TRIGGER {_FILL_TRIGGER} BEFORE {operation} ON {_FILL_TABLE} "
                        "FOR EACH ROW EXECUTE FUNCTION reject_immutable_fill_mutation()"
                    )
                )
            elif damage == "function_only":
                connection.execute(text(f"DROP TRIGGER {_FILL_TRIGGER} ON {_FILL_TABLE}"))
            else:
                raise AssertionError(f"unsupported trigger damage: {damage}")
    finally:
        engine.dispose()


def _damage_journal_guard(database_url: str, damage: str) -> None:
    engine = create_database_engine(database_url)
    try:
        with engine.begin() as connection:
            if damage == "return_new_body":
                connection.execute(
                    text(
                        "CREATE OR REPLACE FUNCTION "
                        "reject_exchange_fill_fact_economic_mutation() "
                        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$"
                    )
                )
            elif damage == "wrong_function":
                connection.execute(
                    text(
                        "CREATE FUNCTION ninth_wrong_journal_guard() RETURNS trigger "
                        "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$"
                    )
                )
                connection.execute(text(f"DROP TRIGGER {_JOURNAL_TRIGGER} ON {_JOURNAL_TABLE}"))
                connection.execute(
                    text(
                        f"CREATE TRIGGER {_JOURNAL_TRIGGER} BEFORE UPDATE OR DELETE "
                        f"ON {_JOURNAL_TABLE} FOR EACH ROW "
                        "EXECUTE FUNCTION ninth_wrong_journal_guard()"
                    )
                )
            elif damage == "disabled_trigger":
                connection.execute(
                    text(f"ALTER TABLE {_JOURNAL_TABLE} DISABLE TRIGGER {_JOURNAL_TRIGGER}")
                )
            else:
                raise AssertionError(f"unsupported journal trigger damage: {damage}")
    finally:
        engine.dispose()


@pytest.mark.postgresql
@pytest.mark.parametrize(
    "damage",
    (
        "ineffective_body",
        "return_new_body",
        "missing_function",
        "wrong_function",
        "wrong_schema",
        "wrong_table",
        "disabled_trigger",
        "update_only",
        "delete_only",
        "function_only",
    ),
)
def test_0015_repairs_damaged_fill_function_or_trigger(
    followup_postgresql_database: str, damage: str
) -> None:
    _upgrade(followup_postgresql_database, PREVIOUS_HEAD)
    _damage_fill_guard(followup_postgresql_database, damage)

    _upgrade(followup_postgresql_database)

    _assert_catalog_contracts(followup_postgresql_database)
    _assert_migration_probe_rolled_back(followup_postgresql_database)
    values = _insert_guarded_rows(followup_postgresql_database)
    _assert_mutations_rejected(followup_postgresql_database, values)
    _assert_apply_state_updates_remain_available(followup_postgresql_database, values)


@pytest.mark.postgresql
@pytest.mark.parametrize("damage", ("return_new_body", "wrong_function", "disabled_trigger"))
def test_0015_repairs_damaged_exchange_fill_fact_journal_guard(
    followup_postgresql_database: str, damage: str
) -> None:
    _upgrade(followup_postgresql_database)
    _damage_journal_guard(followup_postgresql_database, damage)
    _set_alembic_version(followup_postgresql_database, PREVIOUS_HEAD)

    _upgrade(followup_postgresql_database)

    _assert_catalog_contracts(followup_postgresql_database)
    values = _insert_guarded_rows(followup_postgresql_database)
    _assert_mutations_rejected(followup_postgresql_database, values)
    _assert_apply_state_updates_remain_available(followup_postgresql_database, values)


@pytest.mark.postgresql
@pytest.mark.parametrize("damage", ("return_new_body", "disabled_trigger"))
def test_marker_does_not_mask_semantic_function_or_trigger_drift(
    followup_postgresql_database: str, damage: str
) -> None:
    _upgrade(followup_postgresql_database)
    _damage_fill_guard(followup_postgresql_database, damage)
    _set_alembic_version(followup_postgresql_database, PREVIOUS_HEAD)

    _upgrade(followup_postgresql_database)

    _assert_catalog_contracts(followup_postgresql_database)
    values = _insert_guarded_rows(followup_postgresql_database)
    _assert_mutations_rejected(followup_postgresql_database, values)


@pytest.mark.postgresql
@pytest.mark.parametrize(
    ("environment_name", "checkpoint"),
    (
        ("UTA_0015_FAIL_AFTER", "canonical_functions"),
        ("UTA_0015_FAIL_AFTER", "trigger_bindings"),
        ("UTA_0015_FAIL_BEFORE_MARKER", "behavioral_probe"),
    ),
)
def test_0015_recovers_after_checkpoint_crash_before_retry(
    followup_postgresql_database: str,
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    checkpoint: str,
) -> None:
    _upgrade(followup_postgresql_database, PREVIOUS_HEAD)
    monkeypatch.setenv(environment_name, checkpoint)
    with pytest.raises(RuntimeError, match="0015 checkpoint failure"):
        _upgrade(followup_postgresql_database)
    monkeypatch.delenv(environment_name)

    _upgrade(followup_postgresql_database)
    _upgrade(followup_postgresql_database)

    _assert_catalog_contracts(followup_postgresql_database)
    _assert_migration_probe_rolled_back(followup_postgresql_database)
    values = _insert_guarded_rows(followup_postgresql_database)
    _assert_mutations_rejected(followup_postgresql_database, values)


@pytest.mark.postgresql
def test_0015_repairs_security_and_search_path_drift_with_existing_marker(
    followup_postgresql_database: str,
) -> None:
    _upgrade(followup_postgresql_database)
    engine = create_database_engine(followup_postgresql_database)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER FUNCTION reject_immutable_fill_mutation() SECURITY DEFINER")
            )
            connection.execute(
                text("ALTER FUNCTION reject_immutable_fill_mutation() SET search_path = public")
            )
    finally:
        engine.dispose()
    _set_alembic_version(followup_postgresql_database, PREVIOUS_HEAD)

    _upgrade(followup_postgresql_database)

    _assert_catalog_contracts(followup_postgresql_database)


def test_0015_leaves_sqlite_path_unchanged(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'ninth-trigger-followup.db'}"
    _upgrade(database_url)
    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
            triggers = set(
                connection.scalars(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
            )
            assert f"prevent_{_FILL_TABLE}_update" in triggers
            assert f"prevent_{_FILL_TABLE}_delete" in triggers
            assert f"prevent_{_JOURNAL_TABLE}_economic_update" in triggers
            assert f"prevent_{_JOURNAL_TABLE}_delete" in triggers
    finally:
        engine.dispose()
