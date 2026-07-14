from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from sqlalchemy import MetaData, Table, inspect, text

from alembic import command
from app.persistence.audit import AuditDeliveryStatus, AuditRepository
from app.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from app.persistence.replay import ReplayRunner


def test_initial_migration_creates_persistence_tables(tmp_path: Path) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    database_url = f"sqlite:///{tmp_path / 'migration.sqlite'}"
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(config, "head")

    engine = create_database_engine(database_url)
    table_names = set(inspect(engine).get_table_names())
    assert {
        "audit_events",
        "processed_events",
        "trade_plan_projections",
        "reconciliation_runs",
        "durable_order_intents",
    } <= table_names
    durable_columns = {
        column["name"] for column in inspect(engine).get_columns("durable_order_intents")
    }
    assert {
        "economic_key",
        "attempt_number",
        "client_order_id",
        "status",
        "filled_quantity",
    } <= durable_columns
    with engine.connect() as connection:
        trigger_names = set(
            connection.scalars(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                    "AND tbl_name = 'audit_events'"
                )
            )
        )
    assert {"prevent_audit_events_update", "prevent_audit_events_delete"} <= trigger_names
    engine.dispose()


def test_populated_0001_database_upgrades_to_replayable_current_head(tmp_path: Path) -> None:
    """A historical populated database must not be stranded by later audit migrations."""
    backend_root = Path(__file__).resolve().parents[1]
    database_url = f"sqlite:///{tmp_path / 'populated-0001.sqlite'}"
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    occurred_at = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)

    command.upgrade(config, "0001_initial_persistence")
    engine = create_database_engine(database_url)
    metadata = MetaData()
    audit_events = Table("audit_events", metadata, autoload_with=engine)
    processed_events = Table("processed_events", metadata, autoload_with=engine)
    projections = Table("trade_plan_projections", metadata, autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(
            audit_events.insert().values(
                event_id="legacy-event-1",
                source="simulator",
                event_type="state_transition",
                occurred_at=occurred_at,
                payload={"plan_id": "legacy-plan", "to_state": "CANDIDATE"},
                previous_hash=None,
                record_hash="a" * 64,
                created_at=occurred_at,
            )
        )
        connection.execute(
            processed_events.insert().values(
                event_id="legacy-event-1",
                source="simulator",
                first_seen_at=occurred_at,
            )
        )
        connection.execute(
            projections.insert().values(
                plan_id="legacy-plan",
                state="CANDIDATE",
                last_event_id="legacy-event-1",
                updated_at=occurred_at,
            )
        )
    engine.dispose()

    command.upgrade(config, "head")

    upgraded_engine = create_database_engine(database_url)
    repository = AuditRepository(create_session_factory(upgraded_engine))
    replay = ReplayRunner().replay(repository.list_audit_events(), repository.audit_chain_head())
    duplicate = repository.record_delivery(
        event_id="legacy-event-1",
        source="simulator",
        event_type="state_transition",
        occurred_at=occurred_at,
        payload={"plan_id": "legacy-plan", "to_state": "CANDIDATE"},
    )

    assert replay.is_valid
    assert repository.projected_state("legacy-plan") == "CANDIDATE"
    assert duplicate.delivery_status is AuditDeliveryStatus.EXACT_DUPLICATE
    assert {
        "durable_intent_fills",
        "durable_intent_absence_observations",
    } <= set(inspect(upgraded_engine).get_table_names())
    with upgraded_engine.connect() as connection:
        trigger_names = set(
            connection.scalars(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
        )
    assert {
        "prevent_durable_intent_fills_update",
        "prevent_durable_intent_fills_delete",
        "prevent_durable_intent_absence_observations_update",
        "prevent_durable_intent_absence_observations_delete",
    } <= trigger_names
    upgraded_engine.dispose()
