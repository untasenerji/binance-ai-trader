from pathlib import Path

from alembic.config import Config
from sqlalchemy import inspect, text

from alembic import command
from app.persistence.database import create_database_engine


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
