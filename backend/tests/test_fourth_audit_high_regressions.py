"""Red-first counterexamples from the fourth independent audit."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from importlib import import_module
from inspect import signature
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.domain.types import Direction
from app.exchange.contracts import (
    AlgoOrderIntent,
    AlgoOrderType,
    LocalReconciliationState,
    ReconciliationOutcome,
    ReconciliationSnapshot,
    reconcile_local_state,
)
from app.persistence.audit import AuditRepository
from app.persistence.circuit_breaker import (
    PersistenceCircuitBreaker,
    PersistenceRecoveryEvidence,
)
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.persistence.models import DurableAccountPortfolioEnvelope, DurableActualRiskPolicy
from app.persistence.replay import ReplayRunner
from app.planning.fills import (
    AccountPortfolioEnvelope,
    ActualRiskPolicy,
    FillEvent,
    FillLedgerError,
)
from app.simulation.intent_ledger import (
    AbsenceEvidenceSource,
    DurableIntentLedger,
    DurableIntentStatus,
    DurableRiskPolicyError,
    UnknownIntentObservation,
)
from app.simulation.models import (
    OrderRole,
    SimulatedFault,
    SimulatedFill,
    SimulatedOrderIntent,
    SimulatedOrderStatus,
)
from app.simulation.simulator import (
    EntryRiskBlocked,
    ExchangeSimulator,
    FaultPlan,
    FillSequencePlan,
    SimulatedUnknownQueryPlan,
    SimulatedUnknownRemoteState,
    SimulatorError,
    UnknownOrderOutcome,
)
from app.strategy.backtest import (
    BacktestCosts,
    BacktestEngine,
    FundingSettlement,
    WalkForwardRunner,
)
from app.strategy.models import Candle, StrategySpecification
from app.strategy.strategies import VolatilityBreakoutStrategy


def _policy(
    plan_id: str,
    *,
    direction: Direction = Direction.LONG,
    symbol: str = "BTCUSDT",
    max_symbol_exposure: Decimal = Decimal("100"),
    max_total_exposure: Decimal = Decimal("100"),
    equity: Decimal = Decimal("100"),
    risk_budget: Decimal = Decimal("100"),
) -> ActualRiskPolicy:
    return ActualRiskPolicy(
        plan_id=plan_id,
        symbol=symbol,
        direction=direction,
        worst_stop_exit_price=(Decimal("90") if direction is Direction.LONG else Decimal("110")),
        exit_fee_rate=Decimal("0"),
        funding_buffer_rate=Decimal("0"),
        funding_interval_count=0,
        risk_budget=risk_budget,
        effective_leverage=2,
        protective_stop_reference=f"{plan_id}-sim-stop",
        reduce_only_exit_reference=f"{plan_id}-sim-reduce",
        portfolio_envelope=AccountPortfolioEnvelope(
            account_scope="fourth-audit-account",
            version=1,
            verified_account_equity_usdt=equity,
            bot_equity_cap_usdt=equity,
            required_reserve_usdt=Decimal("0"),
            max_total_exposure_usdt=max_total_exposure,
            max_symbol_exposure_usdt=max_symbol_exposure,
            max_required_margin_usdt=equity,
            daily_remaining_risk_usdt=Decimal("1000000"),
            weekly_remaining_risk_usdt=Decimal("1000000"),
            open_position_count=0,
            pending_order_count=0,
        ),
    )


def _entry(
    client_order_id: str,
    plan_id: str,
    *,
    direction: Direction = Direction.LONG,
    stage_index: int = 1,
    quantity: Decimal = Decimal("0.010"),
    price: Decimal = Decimal("100"),
) -> SimulatedOrderIntent:
    return SimulatedOrderIntent(
        client_order_id=client_order_id,
        plan_id=plan_id,
        symbol="BTCUSDT",
        direction=direction,
        role=OrderRole.ENTRY,
        stage_index=stage_index,
        quantity=quantity,
        price=price,
    )


def _fill(
    client_order_id: str,
    trade_id: str,
    *,
    last_quantity: Decimal,
    cumulative_quantity: Decimal,
    fill_price: Decimal,
    fee: Decimal = Decimal("0"),
) -> FillEvent:
    return FillEvent(
        trade_id=trade_id,
        client_order_id=client_order_id,
        last_quantity=last_quantity,
        cumulative_quantity=cumulative_quantity,
        fill_price=fill_price,
        fee=fee,
        fee_asset="USDT",
        occurred_at=datetime(2026, 7, 15, tzinfo=UTC),
    )


@pytest.fixture
def fourth_session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'fourth-audit-high.sqlite'}")
    create_schema(engine)
    return create_session_factory(engine)


def _clean_outcome() -> ReconciliationOutcome:
    return reconcile_local_state(
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


def test_simulated_protection_is_not_exchange_confirmation(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "evidence-boundary"
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(plan_id),
        fill_plan=FillSequencePlan.from_sequences(
            (
                (
                    SimulatedFill(
                        trade_id="evidence-boundary-fill",
                        last_quantity=Decimal("0.010"),
                        cumulative_quantity=Decimal("0.010"),
                        fill_price=Decimal("100"),
                        fee=Decimal("0"),
                        fee_asset="USDT",
                    ),
                ),
            )
        ),
    )
    simulator.submit(_entry("evidence-boundary-entry", plan_id))

    ledger_module = import_module("app.simulation.intent_ledger")
    contracts_module = import_module("app.exchange.contracts")
    simulated_type = ledger_module.SimulatedProtectionEvidence
    exchange_type = contracts_module.ExchangeProtectionEvidence
    gate_type = contracts_module.LiveProtectionEvidenceGate
    evidence = durable_intent_ledger.simulated_protection_evidence(plan_id)

    assert isinstance(evidence, simulated_type)
    assert not isinstance(evidence, exchange_type)
    assert not hasattr(evidence, "stop_confirmed")
    assert not hasattr(evidence, "exchange_position_confirmed")
    with pytest.raises(TypeError, match="exchange protection"):
        gate_type().require(evidence)


def test_recovery_checkpoint_uses_rehearsal_language_only() -> None:
    recovery = import_module("app.security.recovery")

    assert not hasattr(recovery, "StopProtectionEvidence")
    assert {item.value for item in recovery.SimulatedProtectionStatus} == {
        "REHEARSAL_READY",
        "UNPROTECTED",
        "MISSING",
    }
    assert not hasattr(recovery, "SimulatedOpenPosition")


def test_pending_entries_are_included_before_same_plan_acceptance(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "pending-same-plan"
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(
            plan_id,
            direction=Direction.SHORT,
            max_symbol_exposure=Decimal("5"),
            max_total_exposure=Decimal("5"),
            equity=Decimal("2.5"),
        ),
    )
    first = simulator.submit(
        _entry(
            "pending-same-plan-1",
            plan_id,
            direction=Direction.SHORT,
            quantity=Decimal("0.025"),
        )
    )

    assert first.status is SimulatedOrderStatus.NEW
    with pytest.raises(EntryRiskBlocked, match="PENDING|EXPOSURE|MARGIN"):
        simulator.submit(
            _entry(
                "pending-same-plan-2",
                plan_id,
                direction=Direction.SHORT,
                stage_index=2,
                quantity=Decimal("0.026"),
            )
        )


def test_total_exposure_is_aggregated_across_plans(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    durable_intent_ledger.register_actual_risk_policy(
        _policy(
            "aggregate-plan-a", max_symbol_exposure=Decimal("5"), max_total_exposure=Decimal("5")
        )
    )
    durable_intent_ledger.register_actual_risk_policy(
        _policy(
            "aggregate-plan-b", max_symbol_exposure=Decimal("5"), max_total_exposure=Decimal("5")
        )
    )
    simulator = ExchangeSimulator(intent_ledger=durable_intent_ledger)
    simulator.submit(_entry("aggregate-entry-a", "aggregate-plan-a", quantity=Decimal("0.030")))

    with pytest.raises(EntryRiskBlocked, match="AGGREGATE|EXPOSURE|MARGIN"):
        simulator.submit(_entry("aggregate-entry-b", "aggregate-plan-b", quantity=Decimal("0.030")))


def test_better_short_fill_creates_durable_risk_reduction_requirement_and_cancels_pending(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "short-reduction-required"
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(
            plan_id,
            direction=Direction.SHORT,
            max_symbol_exposure=Decimal("5"),
            max_total_exposure=Decimal("5"),
            equity=Decimal("2.5"),
        ),
    )
    pending = simulator.submit(
        _entry(
            "short-pending-stage",
            plan_id,
            direction=Direction.SHORT,
            stage_index=2,
            quantity=Decimal("0.000001"),
            price=Decimal("1"),
        )
    )
    simulator.fill_plan = FillSequencePlan.from_sequences(
        (
            (
                SimulatedFill(
                    trade_id="short-cap-breach-fill",
                    last_quantity=Decimal("0.049999"),
                    cumulative_quantity=Decimal("0.049999"),
                    fill_price=Decimal("100.2"),
                    fee=Decimal("0"),
                    fee_asset="USDT",
                ),
            ),
        )
    )
    filled = simulator.submit(
        _entry(
            "short-filled-stage",
            plan_id,
            direction=Direction.SHORT,
            stage_index=1,
            quantity=Decimal("0.049999"),
        )
    )

    requirement = durable_intent_ledger.risk_reduction_requirement(plan_id)
    restarted = durable_intent_ledger.reopen_after_restart()

    assert filled.status is SimulatedOrderStatus.FILLED
    assert pending.status is SimulatedOrderStatus.CANCELLED
    assert requirement.reason == "ACTUAL_EXPOSURE_LIMIT_BREACH"
    assert requirement.required_reduction_quantity == Decimal("0.049999")
    assert requirement.is_open
    assert restarted.risk_reduction_requirement(plan_id) == requirement


def test_late_fill_beyond_simulated_protection_quantity_hard_blocks_after_restart(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "late-fill-protection-gap"
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(plan_id),
        fill_plan=FillSequencePlan.from_sequences(
            (
                (
                    SimulatedFill(
                        trade_id="protected-partial",
                        last_quantity=Decimal("0.004"),
                        cumulative_quantity=Decimal("0.004"),
                        fill_price=Decimal("100"),
                        fee=Decimal("0.001"),
                        fee_asset="USDT",
                    ),
                ),
            )
        ),
    )
    simulator.submit(_entry("late-fill-gap-entry", plan_id))
    simulator.cancel("late-fill-gap-entry")
    late = _fill(
        "late-fill-gap-entry",
        "late-fill-gap-second",
        last_quantity=Decimal("0.006"),
        cumulative_quantity=Decimal("0.010"),
        fill_price=Decimal("102"),
        fee=Decimal("0.002"),
    )

    durable_intent_ledger.record_fill(late)
    duplicate = durable_intent_ledger.record_fill(late)
    restarted = ExchangeSimulator.reopen_after_restart(
        intent_ledger=durable_intent_ledger.reopen_after_restart()
    )
    risk = restarted.actual_risk(plan_id)

    assert duplicate.is_duplicate
    assert risk.position_quantity == Decimal("0.010")
    assert risk.pending_entries_blocked
    assert risk.hard_halted
    assert risk.reason == "SIMULATED_PROTECTION_QUANTITY_MISMATCH"
    with pytest.raises(EntryRiskBlocked, match="PROTECTION_QUANTITY_MISMATCH"):
        restarted.submit(_entry("late-fill-gap-next", plan_id, stage_index=2))


def _record_complete_absence(
    simulator: ExchangeSimulator,
    intent: SimulatedOrderIntent,
    *,
    unknown_at_ms: int,
) -> None:
    for offset in (0, 1_000):
        observed_at_ms = unknown_at_ms + offset
        simulator.advance_to(observed_at_ms)
        for source in AbsenceEvidenceSource:
            simulator.query_unknown_source(intent.client_order_id, source)


def test_unknown_absent_then_authoritative_late_fill_is_persisted_and_conflicts_are_rejected(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "absent-late-fill"
    intent = _entry("absent-late-fill-entry", plan_id)
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(plan_id),
        fault_plan=FaultPlan.from_faults((SimulatedFault.UNKNOWN_503,)),
        unknown_query_plan=SimulatedUnknownQueryPlan.from_states(
            (SimulatedUnknownRemoteState.ABSENT,)
        ),
        now_ms=2_000,
    )
    with pytest.raises(UnknownOrderOutcome):
        simulator.submit(intent)
    _record_complete_absence(simulator, intent, unknown_at_ms=2_000)
    simulator.resolve_unknown_as_absent(intent.client_order_id)
    late = _fill(
        intent.client_order_id,
        "absent-authoritative-trade",
        last_quantity=Decimal("0.010"),
        cumulative_quantity=Decimal("0.010"),
        fill_price=Decimal("101"),
        fee=Decimal("0.003"),
    )

    receipt = durable_intent_ledger.record_fill(late)
    duplicate = durable_intent_ledger.record_fill(late)
    rebuilt = durable_intent_ledger.reopen_after_restart().fill_ledger_for_plan(plan_id)

    assert not receipt.is_duplicate
    assert duplicate.is_duplicate
    assert durable_intent_ledger.intent(intent.client_order_id).status is DurableIntentStatus.FILLED
    assert rebuilt.filled_quantity == Decimal("0.010")
    assert rebuilt.average_fill_price == Decimal("101")
    assert rebuilt.total_fee == Decimal("0.003")
    with pytest.raises(FillLedgerError, match="semantic conflict"):
        durable_intent_ledger.record_fill(
            _fill(
                intent.client_order_id,
                late.trade_id,
                last_quantity=Decimal("0.010"),
                cumulative_quantity=Decimal("0.010"),
                fill_price=Decimal("102"),
                fee=Decimal("0.003"),
            )
        )


def test_breaker_rejects_an_audit_repository_subclass_override(
    fourth_session_factory: sessionmaker[Session],
) -> None:
    breaker = PersistenceCircuitBreaker()
    ledger = DurableIntentLedger(fourth_session_factory, persistence_breaker=breaker)

    class ForgedAuditRepository(AuditRepository):
        def collect_persistence_recovery_evidence(
            self,
            *,
            intent_ledger: DurableIntentLedger,
            reconciliation_snapshot: ReconciliationSnapshot,
        ) -> PersistenceRecoveryEvidence:
            del intent_ledger, reconciliation_snapshot
            return PersistenceRecoveryEvidence(
                write_probe_event_id="forged-probe",
                audit_event_count=1,
                audit_last_sequence=1,
                audit_last_record_hash="0" * 64,
                replay_valid=True,
                projection_matches_replay=True,
                reconciliation_outcome=_clean_outcome(),
                unresolved_intent_ids=(),
            )

    forged = ForgedAuditRepository(fourth_session_factory, persistence_breaker=breaker)
    with pytest.raises((TypeError, RuntimeError)):
        breaker.reset_after_verified_reconciliation(
            audit_repository=forged,
            intent_ledger=ledger,
            reconciliation_snapshot=ReconciliationSnapshot(
                positions_by_symbol={},
                normal_order_client_ids=frozenset(),
                algo_order_client_ids=frozenset(),
            ),
        )


def test_reconciliation_rejects_wrong_side_stop_for_long_position() -> None:
    wrong_side = AlgoOrderIntent(
        client_algo_id="wrong-side-stop",
        symbol="BTCUSDT",
        direction=Direction.SHORT,
        algo_type=AlgoOrderType.STOP_MARKET,
        trigger_price=Decimal("90"),
        close_position=True,
    )
    outcome = reconcile_local_state(
        local=LocalReconciliationState(
            positions_by_symbol={"BTCUSDT": Decimal("0.010")},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset({wrong_side.client_algo_id}),
            required_stop_symbols=frozenset({"BTCUSDT"}),
            unresolved_unknown_intent_ids=frozenset(),
            audit_chain_valid=True,
            replay_valid=True,
        ),
        snapshot=ReconciliationSnapshot(
            positions_by_symbol={"BTCUSDT": Decimal("0.010")},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset({wrong_side.client_algo_id}),
            algo_orders=(wrong_side,),
        ),
    )

    assert not outcome.is_clean
    assert outcome.missing_stop_symbols == ("BTCUSDT",)


def test_reconciliation_outcome_reasons_are_derived_and_stop_quantity_is_typed() -> None:
    assert "reason_codes" not in signature(ReconciliationOutcome).parameters
    assert "quantity" in signature(AlgoOrderIntent).parameters
    with pytest.raises(ValueError, match="quantity|close"):
        AlgoOrderIntent(
            client_algo_id="invalid-close-position-quantity",
            symbol="BTCUSDT",
            direction=Direction.LONG,
            algo_type=AlgoOrderType.STOP_MARKET,
            trigger_price=Decimal("90"),
            close_position=True,
            quantity=Decimal("0.010"),
        )


def test_unknown_queries_derive_causal_non_future_times(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "unknown-time-causality"
    intent = _entry("unknown-time-entry", plan_id)
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(plan_id),
        fault_plan=FaultPlan.from_faults((SimulatedFault.UNKNOWN_503,)),
        unknown_query_plan=SimulatedUnknownQueryPlan.from_states(
            (SimulatedUnknownRemoteState.ABSENT,)
        ),
        now_ms=1_000,
    )
    with pytest.raises(UnknownOrderOutcome):
        simulator.submit(intent)
    simulator.query_unknown_source(
        intent.client_order_id,
        AbsenceEvidenceSource.NORMAL_OPEN_ORDERS,
    )
    simulator.advance_to(2_000)
    simulator.query_unknown_source(
        intent.client_order_id,
        AbsenceEvidenceSource.TRADE_HISTORY,
    )

    observations = durable_intent_ledger.list_absence_observations(intent.client_order_id)
    assert tuple(item.query_started_at_ms for item in observations) == (1_000, 2_000)
    assert all(item.query_started_at_ms >= 1_000 for item in observations)
    assert all(item.query_started_at_ms <= item.observed_at_ms for item in observations)


def test_unknown_observation_is_bound_to_attempt_namespace_and_unique_provenance() -> None:
    parameters = signature(UnknownIntentObservation).parameters
    assert {"attempt_number", "client_order_namespace", "provenance_fingerprint"} <= set(parameters)


def test_caller_supplied_unknown_observations_have_no_public_persistence_path() -> None:
    assert not hasattr(DurableIntentLedger, "record_absence_observation")
    assert not hasattr(ExchangeSimulator, "record_unknown_absence_observation")
    assert set(signature(ExchangeSimulator.query_unknown_source).parameters) == {
        "self",
        "client_order_id",
        "source",
    }


def test_unknown_query_without_simulated_remote_state_fails_closed(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "unknown-no-query-state"
    intent = _entry("unknown-no-query-state-entry", plan_id)
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(plan_id),
        fault_plan=FaultPlan.from_faults((SimulatedFault.UNKNOWN_503,)),
    )
    with pytest.raises(UnknownOrderOutcome):
        simulator.submit(intent)

    with pytest.raises(SimulatorError, match="unresolved"):
        simulator.query_unknown_source(
            intent.client_order_id,
            AbsenceEvidenceSource.TRADE_HISTORY,
        )

    assert durable_intent_ledger.list_absence_observations(intent.client_order_id) == ()


def _migration_config(database_url: str) -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _populate_adversarial_0001(database_url: str) -> datetime:
    occurred_at = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    command.upgrade(_migration_config(database_url), "0001_initial_persistence")
    engine = create_database_engine(database_url)
    metadata = MetaData()
    audit_events = Table("audit_events", metadata, autoload_with=engine)
    processed_events = Table("processed_events", metadata, autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(
            audit_events.insert().values(
                event_id="legacy-adversarial-transition",
                source="simulator",
                event_type="state_transition",
                occurred_at=occurred_at,
                payload={"plan_id": "legacy-adversarial", "to_state": "NOT_A_STATE"},
                previous_hash=None,
                record_hash="d" * 64,
                created_at=occurred_at,
            )
        )
        connection.execute(
            processed_events.insert().values(
                event_id="legacy-adversarial-transition",
                source="simulator",
                first_seen_at=occurred_at,
            )
        )
    engine.dispose()
    return occurred_at


def _assert_adversarial_upgrade_replays(database_url: str) -> None:
    command.upgrade(_migration_config(database_url), "head")
    engine = create_database_engine(database_url)
    try:
        repository = AuditRepository(create_session_factory(engine))
        replay = ReplayRunner().replay(
            repository.list_audit_events(), repository.audit_chain_head()
        )
        with engine.connect() as connection:
            reconciliation = (
                connection.execute(
                    text(
                        "SELECT status, reason, created_at FROM reconciliation_runs "
                        "WHERE status = 'RECONCILIATION_REQUIRED' ORDER BY created_at LIMIT 1"
                    )
                )
                .mappings()
                .one()
            )
        assert replay.is_valid
        assert reconciliation["status"] == "RECONCILIATION_REQUIRED"
        assert reconciliation["reason"] == "LEGACY_TRANSITION_UNREPLAYABLE"
        assert reconciliation["created_at"] is not None
    finally:
        engine.dispose()


def test_sqlite_adversarial_populated_0001_upgrades_and_replays(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'adversarial-0001.sqlite'}"
    _populate_adversarial_0001(database_url)
    _assert_adversarial_upgrade_replays(database_url)


def test_policy_fingerprint_is_revalidated_when_loaded(
    fourth_session_factory: sessionmaker[Session],
) -> None:
    policy = _policy("fingerprint-reload")
    ledger = DurableIntentLedger(fourth_session_factory)
    with fourth_session_factory.begin() as session:
        envelope = policy.portfolio_envelope
        session.add(
            DurableAccountPortfolioEnvelope(
                account_scope=envelope.account_scope,
                version=envelope.version,
                verified_account_equity_usdt=format(envelope.verified_account_equity_usdt, "f"),
                bot_equity_cap_usdt=format(envelope.bot_equity_cap_usdt, "f"),
                required_reserve_usdt=format(envelope.required_reserve_usdt, "f"),
                max_total_exposure_usdt=format(envelope.max_total_exposure_usdt, "f"),
                max_symbol_exposure_usdt=format(envelope.max_symbol_exposure_usdt, "f"),
                max_required_margin_usdt=format(envelope.max_required_margin_usdt, "f"),
                daily_remaining_risk_usdt=format(envelope.daily_remaining_risk_usdt, "f"),
                weekly_remaining_risk_usdt=format(envelope.weekly_remaining_risk_usdt, "f"),
                open_position_count=envelope.open_position_count,
                pending_order_count=envelope.pending_order_count,
                exposure_slices=[],
                reconciliation_required=False,
                envelope_fingerprint=envelope.fingerprint,
            )
        )
        session.add(
            DurableActualRiskPolicy(
                plan_id=policy.plan_id,
                symbol=policy.symbol,
                direction=policy.direction.value,
                worst_stop_exit_price=format(policy.worst_stop_exit_price, "f"),
                exit_fee_rate=format(policy.exit_fee_rate, "f"),
                funding_buffer_rate=format(policy.funding_buffer_rate, "f"),
                funding_interval_count=policy.funding_interval_count,
                risk_budget=format(policy.risk_budget, "f"),
                max_symbol_exposure_usdt=format(policy.max_symbol_exposure_usdt, "f"),
                max_total_exposure_usdt=format(policy.max_total_exposure_usdt, "f"),
                existing_symbol_exposure_usdt=format(policy.existing_symbol_exposure_usdt, "f"),
                existing_total_exposure_usdt=format(policy.existing_total_exposure_usdt, "f"),
                effective_leverage=policy.effective_leverage,
                required_reserve_usdt=format(policy.required_reserve_usdt, "f"),
                effective_equity_usdt=format(policy.effective_equity_usdt, "f"),
                protective_stop_reference=policy.protective_stop_reference,
                reduce_only_exit_reference=policy.reduce_only_exit_reference,
                account_envelope_scope=envelope.account_scope,
                account_envelope_version=envelope.version,
                account_envelope_fingerprint=envelope.fingerprint,
                policy_fingerprint="0" * 64,
            )
        )

    with pytest.raises(DurableRiskPolicyError, match="fingerprint"):
        ledger.actual_risk_policy(policy.plan_id)


def test_policy_revisions_are_append_only_and_preserve_plan_protection_identity(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    base = _policy("versioned-policy")
    durable_intent_ledger.register_actual_risk_policy(base)
    revised = replace(base, risk_budget=Decimal("50"))

    assert durable_intent_ledger.version_actual_risk_policy(revised) == 2
    assert durable_intent_ledger.reopen_after_restart().actual_risk_policy(base.plan_id) == revised

    with pytest.raises(DurableRiskPolicyError, match="identity|protection"):
        durable_intent_ledger.version_actual_risk_policy(
            replace(revised, protective_stop_reference="changed-reference")
        )

    assert durable_intent_ledger.actual_risk_policy(base.plan_id) == revised


def _candles() -> tuple[Candle, ...]:
    return tuple(
        Candle(
            symbol="BTCUSDT",
            timeframe="1m",
            open_time_ms=index * 60_000,
            close_time_ms=(index + 1) * 60_000 - 1,
            open_price=Decimal("100") + Decimal(index * 2),
            high_price=Decimal("100") + Decimal(index * 2),
            low_price=Decimal("99") + Decimal(index * 2),
            close_price=Decimal("100") + Decimal(index * 2),
            volume=Decimal("1"),
        )
        for index in range(5)
    )


def test_walk_forward_passes_funding_settlements_exactly_like_direct_backtest() -> None:
    candles = _candles()
    costs = BacktestCosts(
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        slippage_bps=Decimal("0"),
    )
    settlement = FundingSettlement(
        symbol="BTCUSDT",
        timeframe="1m",
        settled_at_ms=150_000,
        rate=Decimal("0.01"),
        mark_price=Decimal("104"),
    )
    frozen = VolatilityBreakoutStrategy(lookback=2).fit(candles[:2], timeframe="1m")
    direct = BacktestEngine().run(
        frozen,
        candles,
        timeframe="1m",
        costs=costs,
        evaluation_time_ms=candles[-1].close_time_ms,
        funding_settlements=(settlement,),
    )
    direct_test_trades = tuple(trade for trade in direct.trades if 2 <= trade.entry_bar_index < 5)

    windows = WalkForwardRunner(train_size=2, test_size=3, step_size=3).run(
        StrategySpecification.volatility_breakout(lookback=2),
        candles,
        timeframe="1m",
        costs=costs,
        funding_settlements=(settlement,),
    )

    assert windows[0].result.trades == direct_test_trades
    assert windows[0].result.net_pnl == sum(
        (trade.net_pnl for trade in direct_test_trades), Decimal("0")
    )
    assert any(trade.funding_pnl == Decimal("-1.04") for trade in direct_test_trades)


@pytest.mark.postgresql
def test_postgresql_adversarial_populated_0001_upgrades_and_replays() -> None:
    database_name = f"uta_fourth_migration_{uuid4().hex}"
    database_url = f"postgresql+psycopg://postgres@127.0.0.1:5432/{database_name}"
    admin_engine = create_engine(
        "postgresql+psycopg://postgres@127.0.0.1:5432/postgres",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        _populate_adversarial_0001(database_url)
        _assert_adversarial_upgrade_replays(database_url)
    finally:
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin_engine.dispose()


@pytest.mark.postgresql
def test_postgresql_runtime_cannot_mutate_or_truncate_durable_risk_policy() -> None:
    database_url = "postgresql+psycopg://postgres@127.0.0.1:5432/uta"
    runtime_url = "postgresql+psycopg://uta_runtime@127.0.0.1:5432/uta"
    command.upgrade(_migration_config(database_url), "head")
    admin_engine = create_database_engine(database_url)
    runtime_engine = create_database_engine(runtime_url)
    plan_id = f"runtime-policy-{uuid4().hex}"
    try:
        base = _policy(plan_id)
        DurableIntentLedger(create_session_factory(admin_engine)).register_actual_risk_policy(base)
        runtime_ledger = DurableIntentLedger(create_session_factory(runtime_engine))
        revised = replace(base, risk_budget=Decimal("50"))
        with pytest.raises(DBAPIError):
            runtime_ledger.version_actual_risk_policy(revised)
        admin_ledger = DurableIntentLedger(create_session_factory(admin_engine))
        assert admin_ledger.version_actual_risk_policy(revised) == 2
        assert runtime_ledger.actual_risk_policy(plan_id) == revised
        with runtime_engine.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == "uta_runtime"
            assert not connection.scalar(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            )
            policy_tables = (
                "durable_actual_risk_policies",
                "durable_actual_risk_policy_versions",
            )
            for table_name in policy_tables:
                assert connection.scalar(
                    text("SELECT has_table_privilege(current_user, :table_name, 'SELECT')"),
                    {"table_name": table_name},
                )
                for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    assert not connection.scalar(
                        text("SELECT has_table_privilege(current_user, :table_name, :privilege)"),
                        {"table_name": table_name, "privilege": privilege},
                    )
            mutation_statements = tuple(
                statement
                for table_name in policy_tables
                for statement in (
                    f"INSERT INTO {table_name} SELECT * FROM {table_name} WHERE plan_id = :plan_id",
                    f"UPDATE {table_name} SET risk_budget = '999999' WHERE plan_id = :plan_id",
                    f"DELETE FROM {table_name} WHERE plan_id = :plan_id",
                    f"TRUNCATE {table_name}",
                )
            )
            for statement in mutation_statements:
                with pytest.raises(DBAPIError):
                    connection.execute(text(statement), {"plan_id": plan_id})
                connection.rollback()
    finally:
        runtime_engine.dispose()
        admin_engine.dispose()


@pytest.mark.postgresql
def test_postgresql_risk_reduction_requirement_survives_restart(
    postgresql_session_factory: sessionmaker[Session],
) -> None:
    plan_id = "postgresql-fourth-risk-reduction"
    breaker = PersistenceCircuitBreaker()
    ledger = DurableIntentLedger(postgresql_session_factory, persistence_breaker=breaker)
    repository = AuditRepository(postgresql_session_factory, persistence_breaker=breaker)
    breaker.reset_after_verified_reconciliation(
        audit_repository=repository,
        intent_ledger=ledger,
        reconciliation_snapshot=ReconciliationSnapshot(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
        ),
    )
    ledger.register_actual_risk_policy(
        _policy(
            plan_id,
            direction=Direction.SHORT,
            max_symbol_exposure=Decimal("5"),
            max_total_exposure=Decimal("5"),
            equity=Decimal("2.5"),
        )
    )
    simulator = ExchangeSimulator(
        intent_ledger=ledger,
        fill_plan=FillSequencePlan.from_sequences(
            (
                (
                    SimulatedFill(
                        trade_id="postgresql-fourth-fill",
                        last_quantity=Decimal("0.050"),
                        cumulative_quantity=Decimal("0.050"),
                        fill_price=Decimal("100.2"),
                        fee=Decimal("0"),
                        fee_asset="USDT",
                    ),
                ),
            )
        ),
    )
    simulator.submit(
        _entry(
            "postgresql-fourth-entry",
            plan_id,
            direction=Direction.SHORT,
            quantity=Decimal("0.050"),
        )
    )

    requirement = ledger.reopen_after_restart().risk_reduction_requirement(plan_id)

    assert requirement.is_open
    assert requirement.required_reduction_quantity == Decimal("0.050")
    assert ledger.actual_risk_state(plan_id).pending_entries_blocked
