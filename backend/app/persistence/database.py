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
    """Mirror the migration guards so local test schemas cannot bypass append-only audit rules."""
    with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            connection.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS prevent_audit_events_update "
                    "BEFORE UPDATE ON audit_events "
                    "BEGIN SELECT RAISE(ABORT, 'audit_events are append-only'); END"
                )
            )
            connection.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS prevent_audit_events_delete "
                    "BEFORE DELETE ON audit_events "
                    "BEGIN SELECT RAISE(ABORT, 'audit_events are append-only'); END"
                )
            )
        elif engine.dialect.name == "postgresql":
            connection.execute(
                text(
                    "CREATE OR REPLACE FUNCTION reject_audit_event_mutation() "
                    "RETURNS trigger LANGUAGE plpgsql AS $$ "
                    "BEGIN RAISE EXCEPTION 'audit_events are append-only'; RETURN NULL; END; $$"
                )
            )
            connection.execute(
                text("DROP TRIGGER IF EXISTS prevent_audit_events_mutation ON audit_events")
            )
            connection.execute(
                text(
                    "CREATE TRIGGER prevent_audit_events_mutation "
                    "BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_events "
                    "FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_event_mutation()"
                )
            )
