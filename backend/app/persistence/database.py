"""Engine and session construction kept separate from application startup."""

from sqlalchemy import create_engine
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
