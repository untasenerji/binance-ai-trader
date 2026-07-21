"""Red-first durable state, migration, and PostgreSQL regressions for audit seven."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import MetaData, Table, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.domain.types import Direction
from app.exchange.contracts import ReconciliationSnapshot
from app.persistence.audit import AuditRepository
from app.persistence.circuit_breaker import PersistenceCircuitBreaker, PersistenceUnavailable
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.persistence.models import (
    DurableActualRiskState,
    DurableOrderIntent,
    DurableRiskReductionRequirement,
)
from app.planning.fills import (
    AccountPortfolioEnvelope,
    ActualRiskPolicy,
    FillEvent,
    FillObservationSource,
    FillSide,
)
from app.simulation.intent_ledger import DurableIntentLedger, DurableIntentStatus
from app.simulation.models import OrderRole, SimulatedOrderIntent
from tests.reconciliation_factory import exchange_reconciliation_batch

ACCOUNT_ID = "v1-primary"


def _config(database_url: str) -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _envelope() -> AccountPortfolioEnvelope:
    return AccountPortfolioEnvelope(
        account_scope="seventh-persistence",
        account_id=ACCOUNT_ID,
        version=1,
        verified_account_equity_usdt=Decimal("10"),
        bot_equity_cap_usdt=Decimal("10"),
        required_reserve_usdt=Decimal("0"),
        max_total_exposure_usdt=Decimal("10"),
        max_symbol_exposure_usdt=Decimal("10"),
        symbol_exposure_caps_usdt={"BTCUSDT": Decimal("10")},
        max_required_margin_usdt=Decimal("10"),
        daily_remaining_risk_usdt=Decimal("100"),
        weekly_remaining_risk_usdt=Decimal("100"),
        open_position_count=0,
        pending_order_count=0,
    )


def _policy(plan_id: str) -> ActualRiskPolicy:
    return ActualRiskPolicy(
        plan_id=plan_id,
        symbol="BTCUSDT",
        direction=Direction.LONG,
        worst_stop_exit_price=Decimal("90"),
        exit_fee_rate=Decimal("0"),
        funding_buffer_rate=Decimal("0"),
        funding_interval_count=0,
        risk_budget=Decimal("100"),
        effective_leverage=2,
        protective_stop_reference=f"{plan_id}-stop",
        reduce_only_exit_reference=f"{plan_id}-reduce",
        portfolio_envelope=_envelope(),
    )


def _intent(client_order_id: str, plan_id: str) -> SimulatedOrderIntent:
    return SimulatedOrderIntent(
        account_id=ACCOUNT_ID,
        client_order_id=client_order_id,
        plan_id=plan_id,
        symbol="BTCUSDT",
        direction=Direction.LONG,
        role=OrderRole.ENTRY,
        stage_index=1,
        quantity=Decimal("0.06"),
        price=Decimal("100"),
    )


def _fill(client_order_id: str, trade_id: str) -> FillEvent:
    return FillEvent(
        account_id=ACCOUNT_ID,
        trade_id=trade_id,
        client_order_id=client_order_id,
        symbol="BTCUSDT",
        side=FillSide.BUY,
        last_quantity=Decimal("0.06"),
        cumulative_quantity=Decimal("0.06"),
        fill_price=Decimal("100"),
        fee=Decimal("0"),
        fee_asset="USDT",
        occurred_at=datetime(2026, 7, 16, tzinfo=UTC),
        observation_source=FillObservationSource.SIMULATED_EXCHANGE,
        observation_reference=f"test:{trade_id}",
    )


@pytest.mark.postgresql
def test_role_graph_rejects_transitive_inherit_true_set_false_write_path(
    postgresql_session_factory: sessionmaker[Session],
) -> None:
    """INHERIT can expose writes even when a role cannot be SET explicitly."""
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0012_account_scope_safety.py"
    )
    module_spec = spec_from_file_location("audit_seven_0012", migration_path)
    assert module_spec is not None and module_spec.loader is not None
    migration = module_from_spec(module_spec)
    module_spec.loader.exec_module(migration)
    session_factory = postgresql_session_factory
    suffix = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    bridge = f"uta_seventh_inherit_{suffix}"
    try:
        with session_factory.begin() as session:
            connection = session.connection()
            connection.execute(text(f'CREATE ROLE "{bridge}" NOLOGIN'))
            connection.execute(
                text(
                    f'GRANT uta_policy_config TO "{bridge}" '
                    "WITH INHERIT TRUE, SET FALSE, ADMIN FALSE"
                )
            )
            connection.execute(
                text(f'GRANT "{bridge}" TO uta_runtime WITH INHERIT TRUE, SET FALSE, ADMIN FALSE')
            )
            assert migration._role_graph_violation(connection) == "uta_policy_config"
            with pytest.raises(RuntimeError, match="write role"):
                migration._harden_postgresql_roles(connection)
    finally:
        with session_factory.begin() as session:
            connection = session.connection()
            connection.execute(text(f'REVOKE "{bridge}" FROM uta_runtime'))
            connection.execute(text(f'REVOKE uta_policy_config FROM "{bridge}"'))
            connection.execute(text(f'DROP ROLE IF EXISTS "{bridge}"'))


@pytest.mark.postgresql
def test_postgresql_account_lock_records_unsafe_concurrent_cross_plan_fill_as_reduction(
    postgresql_session_factory: sessionmaker[Session],
) -> None:
    """No pair of independently safe fills may silently exceed the account envelope."""
    session_factory = postgresql_session_factory
    ledger = DurableIntentLedger(session_factory)
    first_plan = "concurrent-first"
    second_plan = "concurrent-second"
    ledger.register_actual_risk_policy(_policy(first_plan))
    ledger.register_actual_risk_policy(_policy(second_plan))
    first = _intent("concurrent-first-entry", first_plan)
    second = _intent("concurrent-second-entry", second_plan)
    with session_factory.begin() as session:
        for intent in (first, second):
            session.add(
                DurableOrderIntent(
                    account_id=ACCOUNT_ID,
                    economic_key=intent.economic_key,
                    attempt_number=1,
                    client_order_id=intent.client_order_id,
                    plan_id=intent.plan_id,
                    symbol=intent.symbol,
                    direction=intent.direction.value,
                    role=intent.role.value,
                    stage_index=intent.stage_index,
                    quantity=format(intent.quantity, "f"),
                    price=format(intent.price, "f"),
                    filled_quantity="0",
                    status=DurableIntentStatus.SUBMITTING.value,
                )
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(
                DurableIntentLedger(session_factory).record_fill,
                _fill(first.client_order_id, "concurrent-fill-one"),
                materialize_simulated_protection=True,
            ),
            executor.submit(
                DurableIntentLedger(session_factory).record_fill,
                _fill(second.client_order_id, "concurrent-fill-two"),
                materialize_simulated_protection=True,
            ),
        )
        for future in futures:
            future.result()

    with session_factory() as session:
        requirements = tuple(session.scalars(select(DurableRiskReductionRequirement)).all())
        positions = tuple(session.scalars(select(DurableActualRiskState)).all())
    assert requirements
    assert any(requirement.status == "OPEN" for requirement in requirements)
    assert sum(Decimal(position.actual_notional_usdt) for position in positions) == Decimal("12")


def test_revocation_append_failure_still_fences_the_old_grant_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'epoch-fence.sqlite'}")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    breaker = PersistenceCircuitBreaker()
    ledger = DurableIntentLedger(session_factory, persistence_breaker=breaker)
    repository = AuditRepository(session_factory, persistence_breaker=breaker)
    breaker.reset_after_verified_reconciliation(
        audit_repository=repository,
        intent_ledger=ledger,
        reconciliation_snapshot=exchange_reconciliation_batch(
            ReconciliationSnapshot(
                account_id=ACCOUNT_ID,
                positions_by_symbol={},
                normal_order_client_ids=frozenset(),
                algo_order_client_ids=frozenset(),
            )
        ),
    )

    def fail_revocation_append(phase: str) -> None:
        if phase == "before_revocation_append":
            raise RuntimeError("injected revocation append outage")

    monkeypatch.setattr(
        DurableIntentLedger,
        "_entry_revocation_checkpoint",
        staticmethod(fail_revocation_append),
    )
    breaker.record_write_failure(RuntimeError("durable write outage"))

    restarted = ledger.reopen_after_restart()
    with pytest.raises(PersistenceUnavailable, match="EPOCH_STALE"):
        restarted.entry_authorization_capability()
    engine.dispose()


@pytest.mark.parametrize(
    "checkpoint",
    ("account_columns", "heads", "backfill", "quarantine_backfill", "guards", "roles"),
)
def test_forward_0012_migration_retries_after_every_checkpoint_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
) -> None:
    database_url = f"sqlite:///{tmp_path / f'forward-0012-retry-{checkpoint}.sqlite'}"
    config = _config(database_url)
    command.upgrade(config, "0011_forward_invariants")
    monkeypatch.setenv("UTA_0012_FAIL_AFTER", checkpoint)

    with pytest.raises(RuntimeError, match="0012 checkpoint"):
        command.upgrade(config, "head")

    monkeypatch.delenv("UTA_0012_FAIL_AFTER")
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {
            "durable_portfolio_envelope_heads",
            "durable_account_safety_states",
            "durable_evidence_quarantines",
            "migration_execution_markers",
        } <= tables
    finally:
        engine.dispose()


@pytest.mark.parametrize("query_reference", ("duplicate-original-reference", None))
def test_populated_0008_invalid_timeline_or_null_provenance_is_quarantined_before_published_0009(
    tmp_path: Path,
    query_reference: str | None,
) -> None:
    """Preflight keeps the original evidence and never rewrites a duplicate reference."""
    database_url = f"sqlite:///{tmp_path / 'preflight-invalid-0008.sqlite'}"
    config = _config(database_url)
    command.upgrade(config, "0008_processed_events_temp")
    engine = create_database_engine(database_url)
    metadata = MetaData()
    intents = Table("durable_order_intents", metadata, autoload_with=engine)
    observations = Table(
        "durable_intent_absence_observations",
        metadata,
        autoload_with=engine,
    )
    try:
        with engine.begin() as session:
            session.execute(
                intents.insert().values(
                    economic_key="preflight-invalid:BTCUSDT:LONG:ENTRY:1",
                    attempt_number=1,
                    client_order_id="preflight-invalid-client",
                    plan_id="preflight-invalid",
                    symbol="BTCUSDT",
                    direction="LONG",
                    role="ENTRY",
                    stage_index=1,
                    quantity="0.01",
                    price="100",
                    filled_quantity="0",
                    status="UNKNOWN",
                    submitted_at_ms=2_000,
                    unknown_at_ms=1_000,
                )
            )
            session.execute(
                observations.insert().values(
                    client_order_id="preflight-invalid-client",
                    economic_key="preflight-invalid:BTCUSDT:LONG:ENTRY:1",
                    source="TRADE_HISTORY",
                    query_reference=query_reference,
                    query_client_order_id="preflight-invalid-client",
                    query_economic_key="preflight-invalid:BTCUSDT:LONG:ENTRY:1",
                    query_started_at_ms=1_000,
                    observed_at_ms=1_500,
                    stream_watermark_ms=1_500,
                    found=False,
                )
            )

        command.upgrade(config, "head")
        with engine.connect() as session:
            evidence = session.scalar(
                text(
                    "SELECT evidence FROM migration_quarantine_records "
                    "WHERE source_identity = 'preflight-invalid-client:1'"
                )
            )
            quarantined_reference = session.scalar(
                text(
                    "SELECT query_reference FROM durable_evidence_quarantines "
                    "WHERE client_order_id = 'preflight-invalid-client'"
                )
            )
            assert evidence is not None
            parsed_evidence = json.loads(evidence) if isinstance(evidence, str) else evidence
            assert parsed_evidence["query_reference"] == query_reference
            assert parsed_evidence["submitted_at_ms"] == 2_000
            assert parsed_evidence["unknown_at_ms"] == 1_000
            assert quarantined_reference == query_reference
            trigger_names = set(
                session.scalars(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
            )
            assert "prevent_durable_intent_absence_observations_update" in trigger_names
            assert "prevent_durable_intent_absence_observations_delete" in trigger_names
    finally:
        engine.dispose()
