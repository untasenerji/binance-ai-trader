"""Acceptance coverage for the least-privilege PostgreSQL runtime role."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import MetaData, Table, text
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.persistence.audit import AuditDeliveryStatus, AuditRepository
from app.persistence.database import create_database_engine, create_session_factory
from app.persistence.replay import ReplayRunner

LOCAL_POSTGRES_TEST_URL = "postgresql+psycopg://postgres@127.0.0.1:5432/uta"
RUNTIME_POSTGRES_TEST_URL = "postgresql+psycopg://uta_runtime@127.0.0.1:5432/uta"


def _upgrade_postgresql_to_head() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", LOCAL_POSTGRES_TEST_URL)
    command.upgrade(config, "head")


def _migration_config(database_url: str) -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.mark.postgresql
def test_postgresql_runtime_role_has_only_required_dml_and_no_ddl() -> None:
    _upgrade_postgresql_to_head()
    engine = create_database_engine(RUNTIME_POSTGRES_TEST_URL)
    try:
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT has_schema_privilege(current_user, 'public', 'USAGE')")
            )
            assert not connection.scalar(
                text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
            )
            assert connection.scalar(
                text("SELECT has_table_privilege(current_user, 'audit_events', 'INSERT')")
            )
            assert connection.scalar(
                text("SELECT has_table_privilege(current_user, 'audit_chain_heads', 'UPDATE')")
            )
            for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
                assert not connection.scalar(
                    text("SELECT has_table_privilege(current_user, 'audit_events', :privilege)"),
                    {"privilege": privilege},
                )
            assert connection.scalar(text("SELECT COUNT(*) FROM audit_events")) is not None

        repository = AuditRepository(create_session_factory(engine))
        receipt = repository.record_delivery(
            event_id=f"runtime-role-probe-{uuid4().hex}",
            source="acceptance",
            event_type="runtime_role_probe",
            occurred_at=datetime(2026, 7, 13, tzinfo=UTC),
            payload={"scope": "least-privilege"},
        )
        assert receipt.delivery_status is AuditDeliveryStatus.CANONICAL

        for statement in (
            "UPDATE audit_events SET source = source WHERE FALSE",
            "DELETE FROM audit_events WHERE FALSE",
            "TRUNCATE audit_events",
            "CREATE TABLE runtime_role_forbidden_probe (id integer)",
            "ALTER TABLE audit_events ADD COLUMN runtime_role_forbidden integer",
        ):
            with engine.connect() as connection:
                with pytest.raises(DBAPIError):
                    connection.execute(text(statement))
                connection.rollback()
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_populated_postgresql_0001_database_upgrades_to_replayable_head() -> None:
    database_name = f"uta_migration_{uuid4().hex}"
    database_url = f"postgresql+psycopg://postgres@127.0.0.1:5432/{database_name}"
    admin_engine = create_database_engine(LOCAL_POSTGRES_TEST_URL)
    database_engine = None
    try:
        with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        config = _migration_config(database_url)
        occurred_at = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
        command.upgrade(config, "0001_initial_persistence")
        database_engine = create_database_engine(database_url)
        metadata = MetaData()
        audit_events = Table("audit_events", metadata, autoload_with=database_engine)
        processed_events = Table("processed_events", metadata, autoload_with=database_engine)
        projections = Table("trade_plan_projections", metadata, autoload_with=database_engine)
        with database_engine.begin() as connection:
            connection.execute(
                audit_events.insert().values(
                    event_id="legacy-postgres-event-1",
                    source="simulator",
                    event_type="state_transition",
                    occurred_at=occurred_at,
                    payload={"plan_id": "legacy-postgres-plan", "to_state": "CANDIDATE"},
                    previous_hash=None,
                    record_hash="b" * 64,
                    created_at=occurred_at,
                )
            )
            connection.execute(
                processed_events.insert().values(
                    event_id="legacy-postgres-event-1",
                    source="simulator",
                    first_seen_at=occurred_at,
                )
            )
            connection.execute(
                projections.insert().values(
                    plan_id="legacy-postgres-plan",
                    state="CANDIDATE",
                    last_event_id="legacy-postgres-event-1",
                    updated_at=occurred_at,
                )
            )
        database_engine.dispose()
        database_engine = create_database_engine(database_url)

        command.upgrade(config, "head")
        repository = AuditRepository(create_session_factory(database_engine))
        replay = ReplayRunner().replay(
            repository.list_audit_events(), repository.audit_chain_head()
        )
        duplicate = repository.record_delivery(
            event_id="legacy-postgres-event-1",
            source="simulator",
            event_type="state_transition",
            occurred_at=occurred_at,
            payload={"plan_id": "legacy-postgres-plan", "to_state": "CANDIDATE"},
        )

        assert replay.is_valid
        assert repository.projected_state("legacy-postgres-plan") == "CANDIDATE"
        assert duplicate.delivery_status is AuditDeliveryStatus.EXACT_DUPLICATE
    finally:
        if database_engine is not None:
            database_engine.dispose()
        with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin_engine.dispose()
