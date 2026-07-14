"""Engine and session construction kept separate from application startup."""

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.persistence.models import Base


def create_database_engine(database_url: str) -> Engine:
    if not database_url:
        raise ValueError("database_url is required")
    return create_engine(
        database_url, future=True, pool_pre_ping=not database_url.startswith("sqlite")
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_schema(engine: Engine) -> None:
    """Development/test helper; production schema changes use Alembic migrations."""
    Base.metadata.create_all(engine)
    _install_audit_append_only_guards(engine)


def _install_audit_append_only_guards(engine: Engine) -> None:
    """Mirror immutable-evidence migration guards in local test schemas."""
    with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            for table_name in (
                "audit_events",
                "durable_intent_fills",
                "durable_intent_absence_observations",
            ):
                for operation in ("update", "delete"):
                    connection.execute(
                        text(
                            f"CREATE TRIGGER IF NOT EXISTS prevent_{table_name}_{operation} "
                            f"BEFORE {operation.upper()} ON {table_name} "
                            f"BEGIN SELECT RAISE(ABORT, '{table_name} are append-only'); END"
                        )
                    )
        elif engine.dialect.name == "postgresql":
            connection.execute(
                text(
                    "CREATE OR REPLACE FUNCTION reject_immutable_evidence_mutation() "
                    "RETURNS trigger LANGUAGE plpgsql AS $$ "
                    "BEGIN RAISE EXCEPTION 'immutable evidence is append-only'; "
                    "RETURN NULL; END; $$"
                )
            )
            for table_name, trigger_name in (
                ("audit_events", "prevent_audit_events_mutation"),
                ("durable_intent_fills", "prevent_durable_intent_fills_mutation"),
                (
                    "durable_intent_absence_observations",
                    "prevent_durable_intent_absence_observations_mutation",
                ),
            ):
                connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}"))
                connection.execute(
                    text(
                        f"CREATE TRIGGER {trigger_name} "
                        f"BEFORE UPDATE OR DELETE OR TRUNCATE ON {table_name} "
                        "FOR EACH STATEMENT EXECUTE FUNCTION reject_immutable_evidence_mutation()"
                    )
                )
