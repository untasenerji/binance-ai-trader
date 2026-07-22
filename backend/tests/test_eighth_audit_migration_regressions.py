"""Forward-only migration and retry acceptance for eighth-audit hardening."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, text

from alembic import command
from app.persistence.database import create_database_engine, create_session_factory
from app.simulation.intent_ledger import (
    DurableIntentLedger,
    VerifiedQuarantineResolutionEvidence,
)

HEAD = "0014_durable_execution_facts"
PREVIOUS_HEAD = "0013_execution_safety_core"
START_REVISIONS = (
    "0008_processed_events_temp",
    "0009_evidence_risk_hardening",
    "0011_forward_invariants",
    "0012_account_scope_safety",
)


def _config(database_url: str) -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _assert_0014_postconditions(database_url: str) -> None:
    engine = create_database_engine(database_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {
            "durable_evidence_quarantine_sources",
            "durable_evidence_quarantine_source_resolutions",
            "durable_adapter_query_receipts",
            "durable_entry_admission_decisions",
            "exchange_fill_fact_journal",
        } <= tables
        fill_columns = {
            str(column["name"]): column for column in inspector.get_columns("durable_intent_fills")
        }
        for column_name in (
            "symbol",
            "side",
            "observation_source",
            "observation_reference",
        ):
            assert column_name in fill_columns
            assert not fill_columns[column_name]["nullable"]

        source_table = "durable_evidence_quarantine_sources"
        source_columns = {
            str(column["name"]): column for column in inspector.get_columns(source_table)
        }
        assert source_columns["query_reference"]["nullable"]
        assert all(
            not column["nullable"]
            for name, column in source_columns.items()
            if name != "query_reference"
        )
        assert not source_columns["economic_key"]["nullable"]
        assert not source_columns["attempt_id"]["nullable"]
        index_names = {str(index["name"]) for index in inspector.get_indexes(source_table)}
        assert {
            f"ix_{source_table}_quarantine_id",
            f"ix_{source_table}_account_id",
        } <= index_names
        unique_sets = {
            tuple(str(column) for column in constraint["column_names"])
            for constraint in inspector.get_unique_constraints(source_table)
        }
        assert ("source_kind", "source_table", "source_row_id") in unique_sets
        assert any(
            tuple(key.get("constrained_columns", ())) == ("quarantine_id",)
            and key.get("referred_table") == "durable_evidence_quarantines"
            for key in inspector.get_foreign_keys(source_table)
        )
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
            previous_markers = set(
                connection.scalars(
                    text(
                        "SELECT checkpoint FROM migration_execution_markers "
                        "WHERE migration_revision = :revision"
                    ),
                    {"revision": PREVIOUS_HEAD},
                )
            )
            assert {
                "fill_fact_identity",
                "quarantine_source_links",
                "runtime_policy_acl",
            } <= previous_markers
            markers = set(
                connection.scalars(
                    text(
                        "SELECT checkpoint FROM migration_execution_markers "
                        "WHERE migration_revision = :revision"
                    ),
                    {"revision": HEAD},
                )
            )
            assert {
                "execution_tables",
                "source_identity",
                "grant_receipts",
                "fill_guards",
                "execution_evidence_guards",
            } <= markers
            if engine.dialect.name == "sqlite":
                triggers = set(
                    connection.scalars(
                        text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
                    )
                )
                assert {
                    f"prevent_{source_table}_update",
                    f"prevent_{source_table}_delete",
                } <= triggers
            else:
                assert connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_trigger triggers "
                        "JOIN pg_class tables ON tables.oid = triggers.tgrelid "
                        "WHERE tables.relname = :table_name "
                        "AND triggers.tgname = :trigger_name "
                        "AND NOT triggers.tgisinternal)"
                    ),
                    {
                        "table_name": source_table,
                        "trigger_name": f"prevent_{source_table}_mutation",
                    },
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize("start_revision", START_REVISIONS)
def test_sqlite_each_supported_revision_upgrades_to_0014(
    tmp_path: Path,
    start_revision: str,
) -> None:
    database_url = f"sqlite:///{tmp_path / f'{start_revision}.sqlite'}"
    config = _config(database_url)
    command.upgrade(config, start_revision)
    command.upgrade(config, "head")

    _assert_0014_postconditions(database_url)


@pytest.mark.parametrize(
    ("environment_name", "failure_point"),
    (
        ("UTA_0013_FAIL_AFTER", "fill_fact_identity"),
        ("UTA_0013_FAIL_AFTER", "quarantine_source_links"),
        ("UTA_0013_FAIL_AFTER", "runtime_policy_acl"),
        ("UTA_0013_FAIL_MID_OPERATION", "fill:symbol"),
        ("UTA_0013_FAIL_MID_OPERATION", "fill:side"),
        ("UTA_0013_FAIL_MID_OPERATION", "fill:observation_source"),
        ("UTA_0013_FAIL_MID_OPERATION", "fill:observation_reference"),
        ("UTA_0013_FAIL_MID_OPERATION", "quarantine:table"),
        ("UTA_0013_FAIL_MID_OPERATION", "quarantine:indexes"),
        ("UTA_0013_FAIL_MID_OPERATION", "quarantine:backfill"),
        ("UTA_0013_FAIL_MID_OPERATION", "quarantine:guard"),
    ),
)
def test_sqlite_0013_retries_every_checkpoint_and_mid_operation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    failure_point: str,
) -> None:
    safe_name = failure_point.replace(":", "-")
    database_url = f"sqlite:///{tmp_path / f'retry-{safe_name}.sqlite'}"
    config = _config(database_url)
    command.upgrade(config, "0012_account_scope_safety")
    monkeypatch.setenv(environment_name, failure_point)

    with pytest.raises(RuntimeError, match="0013"):
        command.upgrade(config, "head")

    monkeypatch.delenv(environment_name)
    command.upgrade(config, "head")
    _assert_0014_postconditions(database_url)


def test_sqlite_0013_repairs_marker_ahead_missing_index_and_missing_guard(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'marker-ahead.sqlite'}"
    config = _config(database_url)
    command.upgrade(config, "0012_account_scope_safety")
    engine = create_database_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO migration_execution_markers "
                    "(migration_revision, checkpoint) "
                    "VALUES (:revision, 'quarantine_source_links')"
                ),
                {"revision": PREVIOUS_HEAD},
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    source_table = "durable_evidence_quarantine_sources"
    try:
        with engine.begin() as connection:
            connection.execute(text(f"DROP INDEX ix_{source_table}_account_id"))
            connection.execute(text(f"DROP TRIGGER prevent_{source_table}_update"))
            connection.execute(
                text("UPDATE alembic_version SET version_num = '0012_account_scope_safety'")
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    _assert_0014_postconditions(database_url)


@pytest.fixture
def eighth_postgresql_database() -> Iterator[str]:
    database_name = f"uta_eighth_migration_{uuid4().hex}"
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


@pytest.mark.postgresql
@pytest.mark.parametrize("start_revision", START_REVISIONS)
def test_postgresql_each_supported_revision_upgrades_to_0014(
    eighth_postgresql_database: str,
    start_revision: str,
) -> None:
    config = _config(eighth_postgresql_database)
    command.upgrade(config, start_revision)
    command.upgrade(config, "head")

    _assert_0014_postconditions(eighth_postgresql_database)


@pytest.mark.postgresql
@pytest.mark.parametrize(
    ("environment_name", "failure_point"),
    (
        ("UTA_0013_FAIL_AFTER", "quarantine_source_links"),
        ("UTA_0013_FAIL_AFTER", "runtime_policy_acl"),
        ("UTA_0013_FAIL_MID_OPERATION", "fill:symbol"),
        ("UTA_0013_FAIL_MID_OPERATION", "quarantine:backfill"),
    ),
)
def test_postgresql_0013_failure_is_retryable(
    eighth_postgresql_database: str,
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    failure_point: str,
) -> None:
    config = _config(eighth_postgresql_database)
    command.upgrade(config, "0012_account_scope_safety")
    monkeypatch.setenv(environment_name, failure_point)
    with pytest.raises(RuntimeError, match="0013"):
        command.upgrade(config, "head")

    monkeypatch.delenv(environment_name)
    command.upgrade(config, "head")
    _assert_0014_postconditions(eighth_postgresql_database)


@pytest.mark.postgresql
def test_postgresql_many_to_one_quarantine_sources_require_independent_resolution(
    eighth_postgresql_database: str,
) -> None:
    config = _config(eighth_postgresql_database)
    command.upgrade(config, "0011_forward_invariants")
    engine = create_database_engine(eighth_postgresql_database)
    metadata = MetaData()
    legacy = Table("migration_quarantine_records", metadata, autoload_with=engine)
    economic_key = "postgres-many-to-one:BTCUSDT:SHORT:ENTRY:1"
    try:
        with engine.begin() as connection:
            for index, query_reference in enumerate(("pg-query-one", "pg-query-two"), 1):
                connection.execute(
                    legacy.insert().values(
                        migration_revision="test-eighth-postgres",
                        source_table="durable_order_intents",
                        source_identity=f"postgres-legacy-row-{index}",
                        reason="INVALID_LEGACY_TIMELINE",
                        evidence={
                            "account_id": "v1-primary",
                            "client_order_id": "postgres-many-to-one-entry",
                            "economic_key": economic_key,
                            "provenance_fingerprint": "9" * 64,
                            "query_reference": query_reference,
                        },
                        reconciliation_required=True,
                    )
                )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(eighth_postgresql_database)
    session_factory = create_session_factory(engine)
    ledger = DurableIntentLedger(session_factory)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM durable_evidence_quarantines")) == 1
            assert (
                connection.scalar(text("SELECT COUNT(*) FROM durable_evidence_quarantine_sources"))
                == 2
            )
            quarantine_id = connection.scalar(
                text("SELECT quarantine_id FROM durable_evidence_quarantines")
            )
            source_row_ids = tuple(
                connection.scalars(
                    text(
                        "SELECT source_row_id FROM durable_evidence_quarantine_sources "
                        "ORDER BY source_row_id"
                    )
                )
            )
        assert isinstance(quarantine_id, str)
        assert len(source_row_ids) == 2
        with session_factory() as session:
            assert (
                ledger._unresolved_quarantine_count(  # noqa: SLF001
                    session,
                    account_id="v1-primary",
                )
                == 1
            )
        ambiguous_evidence = VerifiedQuarantineResolutionEvidence(
            account_id="v1-primary",
            economic_key=economic_key,
            client_order_id="postgres-many-to-one-entry",
            query_reference="postgres-fresh-query",
            observed_at=datetime.now(UTC),
        )
        with pytest.raises(ValueError, match="one exact source row identity"):
            ledger.resolve_evidence_quarantine(
                quarantine_id,
                operator_id="postgres-operator-eight",
                verified_evidence=ambiguous_evidence,
            )
        first_evidence = VerifiedQuarantineResolutionEvidence(
            account_id="v1-primary",
            economic_key=economic_key,
            client_order_id="postgres-many-to-one-entry",
            attempt_id="attempt-1",
            source_row_id=source_row_ids[0],
            query_reference="postgres-fresh-query-one",
            observed_at=datetime.now(UTC),
        )
        resolution_id = ledger.resolve_evidence_quarantine(
            quarantine_id,
            operator_id="postgres-operator-eight",
            verified_evidence=first_evidence,
        )
        assert resolution_id == ledger.resolve_evidence_quarantine(
            quarantine_id,
            operator_id="postgres-operator-eight",
            verified_evidence=first_evidence,
        )
        with session_factory() as session:
            assert (
                ledger._unresolved_quarantine_count(  # noqa: SLF001
                    session,
                    account_id="v1-primary",
                )
                == 1
            )
        second_evidence = VerifiedQuarantineResolutionEvidence(
            account_id="v1-primary",
            economic_key=economic_key,
            client_order_id="postgres-many-to-one-entry",
            attempt_id="attempt-1",
            source_row_id=source_row_ids[1],
            query_reference="postgres-fresh-query-two",
            observed_at=datetime.now(UTC),
        )
        ledger.resolve_evidence_quarantine(
            quarantine_id,
            operator_id="postgres-operator-eight",
            verified_evidence=second_evidence,
        )
        restarted = ledger.reopen_after_restart()
        with session_factory() as session:
            assert (
                restarted._unresolved_quarantine_count(  # noqa: SLF001
                    session,
                    account_id="v1-primary",
                )
                == 0
            )
    finally:
        engine.dispose()
