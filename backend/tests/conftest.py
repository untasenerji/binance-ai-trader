from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.domain.types import Direction
from app.exchange.contracts import ReconciliationSnapshot
from app.persistence.audit import AuditRepository
from app.persistence.circuit_breaker import PersistenceCircuitBreaker
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.persistence.models import Base
from app.planning.fills import AccountPortfolioEnvelope, ActualRiskPolicy
from app.simulation.intent_ledger import DurableIntentLedger

LOCAL_POSTGRES_TEST_URL = "postgresql+psycopg://postgres@127.0.0.1:5432/uta"


def actual_risk_policy_for(
    plan_id: str,
    *,
    direction: Direction = Direction.LONG,
    symbol: str = "BTCUSDT",
    worst_stop_exit_price: Decimal = Decimal("1"),
    risk_budget: Decimal = Decimal("100000"),
) -> ActualRiskPolicy:
    """A deliberately roomy but complete policy for simulator behavior tests."""
    return ActualRiskPolicy(
        plan_id=plan_id,
        symbol=symbol,
        direction=direction,
        worst_stop_exit_price=worst_stop_exit_price,
        exit_fee_rate=Decimal("0"),
        funding_buffer_rate=Decimal("0"),
        funding_interval_count=0,
        risk_budget=risk_budget,
        effective_leverage=2,
        protective_stop_reference=f"{plan_id}-simulated-stop",
        reduce_only_exit_reference=f"{plan_id}-simulated-reduce-only-exit",
        portfolio_envelope=AccountPortfolioEnvelope(
            account_scope="test-account",
            version=1,
            verified_account_equity_usdt=Decimal("100000"),
            bot_equity_cap_usdt=Decimal("100000"),
            required_reserve_usdt=Decimal("0"),
            max_total_exposure_usdt=Decimal("100000"),
            max_symbol_exposure_usdt=Decimal("100000"),
            max_required_margin_usdt=Decimal("100000"),
            daily_remaining_risk_usdt=Decimal("100000"),
            weekly_remaining_risk_usdt=Decimal("100000"),
            open_position_count=0,
            pending_order_count=0,
        ),
    )


@pytest.fixture
def durable_intent_ledger(tmp_path: Path) -> Iterator[DurableIntentLedger]:
    """A local SQLite outbox with an explicitly verified test-only persistence gate."""
    engine = create_database_engine(f"sqlite:///{tmp_path / 'durable-intent-fixture.sqlite'}")
    create_schema(engine)
    breaker = PersistenceCircuitBreaker()
    session_factory = create_session_factory(engine)
    ledger = DurableIntentLedger(session_factory, persistence_breaker=breaker)
    repository = AuditRepository(session_factory, persistence_breaker=breaker)
    breaker.reset_after_verified_reconciliation(
        audit_repository=repository,
        intent_ledger=ledger,
        reconciliation_snapshot=ReconciliationSnapshot(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
        ),
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
