from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.exchange.contracts import (
    LocalReconciliationState,
    ReconciliationSnapshot,
    reconcile_local_state,
)
from app.persistence.audit import AuditRepository
from app.persistence.circuit_breaker import PersistenceCircuitBreaker
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.persistence.models import Base
from app.simulation.intent_ledger import DurableIntentLedger

LOCAL_POSTGRES_TEST_URL = "postgresql+psycopg://postgres@127.0.0.1:5432/uta"


@pytest.fixture
def durable_intent_ledger(tmp_path: Path) -> Iterator[DurableIntentLedger]:
    """A local SQLite outbox with an explicitly verified test-only persistence gate."""
    engine = create_database_engine(f"sqlite:///{tmp_path / 'durable-intent-fixture.sqlite'}")
    create_schema(engine)
    clean_reconciliation = reconcile_local_state(
        local=LocalReconciliationState(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
            required_stop_symbols=frozenset(),
            unresolved_unknown_intent_ids=frozenset(),
            audit_chain_valid=True,
            replay_valid=True,
        ),
        snapshot=ReconciliationSnapshot(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
        ),
    )
    breaker = PersistenceCircuitBreaker()
    session_factory = create_session_factory(engine)
    ledger = DurableIntentLedger(session_factory, persistence_breaker=breaker)
    repository = AuditRepository(session_factory, persistence_breaker=breaker)
    breaker.reset_after_verified_reconciliation(
        audit_repository=repository,
        intent_ledger=ledger,
        reconciliation_outcome=clean_reconciliation,
    )
    try:
        yield ledger
    finally:
        engine.dispose()


@pytest.fixture
def postgresql_session_factory() -> Iterator[sessionmaker[Session]]:
    """Provide an isolated local PostgreSQL schema without reading any secret."""
    schema = f"audit_test_{uuid4().hex}"
    base_engine = create_database_engine(LOCAL_POSTGRES_TEST_URL)
    try:
        with base_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        translated_engine = base_engine.execution_options(schema_translate_map={None: schema})
        Base.metadata.create_all(translated_engine)
        yield create_session_factory(translated_engine)
    except Exception as error:
        raise RuntimeError("local PostgreSQL acceptance service is unavailable") from error
    finally:
        if schema.startswith("audit_test_"):
            with base_engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        base_engine.dispose()
