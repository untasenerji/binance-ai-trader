from pathlib import Path

from alembic.config import Config
from sqlalchemy import inspect

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
    } <= table_names
    engine.dispose()
