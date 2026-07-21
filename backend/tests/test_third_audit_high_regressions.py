"""High-severity regression cases from the third independent audit.

Each case is intentionally written against the required fail-closed contract before
the corresponding implementation change is made.
"""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.domain.types import Direction
from app.exchange.contracts import (
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
from app.persistence.replay import ReplayRunner
from app.planning.fills import (
    AccountPortfolioEnvelope,
    ActualRiskPolicy,
    ExposureSourceState,
    FillEvent,
    FillLedgerError,
    FillObservationSource,
    FillSide,
    PortfolioExposureSlice,
)
from app.simulation.intent_ledger import (
    AbsenceEvidenceSource,
    BoundedAbsenceEvidence,
    DurableIntentLedger,
    DurableIntentStatus,
    DurableRiskPolicyError,
    IntentLifecycleError,
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
    UnknownOrderOutcome,
)
from tests.reconciliation_factory import exchange_reconciliation_batch


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'third-audit-high.sqlite'}")
    create_schema(engine)
    return create_session_factory(engine)


def _intent(
    *,
    client_order_id: str,
    plan_id: str,
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


def _policy(
    *,
    plan_id: str,
    direction: Direction,
    worst_stop_exit_price: Decimal,
    risk_budget: Decimal,
    max_symbol_exposure_usdt: Decimal = Decimal("100"),
    max_total_exposure_usdt: Decimal = Decimal("100"),
    existing_symbol_exposure_usdt: Decimal = Decimal("0"),
    existing_total_exposure_usdt: Decimal = Decimal("0"),
    effective_equity_usdt: Decimal = Decimal("50"),
) -> ActualRiskPolicy:
    exposure_slices: tuple[PortfolioExposureSlice, ...] = ()
    if existing_total_exposure_usdt > 0:
        exposure_slices = (
            PortfolioExposureSlice(
                slice_id="external-confirmed",
                plan_id="external-plan",
                symbol="BTCUSDT",
                direction=direction,
                notional_usdt=existing_total_exposure_usdt,
                leverage=2,
                required_margin_usdt=existing_total_exposure_usdt / Decimal(2),
                source_state=ExposureSourceState.EXTERNAL_CONFIRMED,
            ),
        )
    return ActualRiskPolicy(
        plan_id=plan_id,
        symbol="BTCUSDT",
        direction=direction,
        worst_stop_exit_price=worst_stop_exit_price,
        exit_fee_rate=Decimal("0"),
        funding_buffer_rate=Decimal("0"),
        funding_interval_count=0,
        risk_budget=risk_budget,
        effective_leverage=2,
        protective_stop_reference=f"{plan_id}-stop",
        reduce_only_exit_reference=f"{plan_id}-reduce-only-exit",
        portfolio_envelope=AccountPortfolioEnvelope(
            account_scope="third-audit-account",
            version=1,
            verified_account_equity_usdt=effective_equity_usdt,
            bot_equity_cap_usdt=effective_equity_usdt,
            required_reserve_usdt=Decimal("0"),
            max_total_exposure_usdt=max_total_exposure_usdt,
            max_symbol_exposure_usdt=max_symbol_exposure_usdt,
            max_required_margin_usdt=effective_equity_usdt,
            daily_remaining_risk_usdt=Decimal("1000000"),
            weekly_remaining_risk_usdt=Decimal("1000000"),
            open_position_count=0,
            pending_order_count=0,
            exposure_slices=exposure_slices,
        ),
    )


def test_short_better_fill_revalidates_exposure_and_margin_after_fill(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "short-better-fill"
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(
            plan_id=plan_id,
            direction=Direction.SHORT,
            worst_stop_exit_price=Decimal("101"),
            risk_budget=Decimal("1"),
            max_symbol_exposure_usdt=Decimal("5"),
            max_total_exposure_usdt=Decimal("5"),
            effective_equity_usdt=Decimal("2.5"),
        ),
        fill_plan=FillSequencePlan.from_sequences(
            (
                (
                    SimulatedFill(
                        trade_id="short-better-fill-trade",
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

    order = simulator.submit(
        _intent(
            client_order_id="short-better-fill-entry",
            plan_id=plan_id,
            direction=Direction.SHORT,
            quantity=Decimal("0.050"),
        )
    )
    risk = simulator.actual_risk(plan_id)

    assert order.status is SimulatedOrderStatus.FILLED
    assert risk.actual_notional_usdt == Decimal("5.010")
    assert risk.actual_required_margin_usdt == Decimal("2.505")
    assert risk.pending_entries_blocked
    assert risk.reason == "ACTUAL_EXPOSURE_LIMIT_BREACH"


def test_actual_risk_policy_block_confirmed_position_and_protection_survive_restart(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "durable-risk-restart"
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(
            plan_id=plan_id,
            direction=Direction.LONG,
            worst_stop_exit_price=Decimal("90"),
            risk_budget=Decimal("0.1"),
        ),
        fill_plan=FillSequencePlan.from_sequences(
            (
                (
                    SimulatedFill(
                        trade_id="restart-risk-fill",
                        last_quantity=Decimal("0.005"),
                        cumulative_quantity=Decimal("0.005"),
                        fill_price=Decimal("120"),
                        fee=Decimal("0"),
                        fee_asset="USDT",
                    ),
                ),
            )
        ),
    )
    simulator.submit(
        _intent(client_order_id="restart-risk-entry-1", plan_id=plan_id, stage_index=1)
    )

    protection = durable_intent_ledger.simulated_protection_evidence(plan_id)
    restarted = ExchangeSimulator.reopen_after_restart(
        intent_ledger=durable_intent_ledger.reopen_after_restart()
    )
    recovered_risk = restarted.actual_risk(plan_id)

    assert protection.stop_intent_ready
    assert protection.reduce_only_exit_intent_ready
    assert recovered_risk.position_quantity == Decimal("0.005")
    assert recovered_risk.pending_entries_blocked
    with pytest.raises(EntryRiskBlocked, match="ACTUAL_STOP_RISK_BREACH"):
        restarted.submit(
            _intent(client_order_id="restart-risk-entry-2", plan_id=plan_id, stage_index=2)
        )


def test_entry_without_a_matching_durable_policy_fails_closed(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    simulator = ExchangeSimulator(intent_ledger=durable_intent_ledger)

    with pytest.raises(EntryRiskBlocked, match="RISK_POLICY_MISSING"):
        simulator.submit(_intent(client_order_id="missing-policy-entry", plan_id="missing-policy"))


def _fill(
    *,
    trade_id: str,
    client_order_id: str,
    last_quantity: Decimal,
    cumulative_quantity: Decimal,
    fill_price: Decimal,
    fee: Decimal,
) -> FillEvent:
    return FillEvent(
        account_id="v1-primary",
        trade_id=trade_id,
        client_order_id=client_order_id,
        symbol="BTCUSDT",
        side=FillSide.BUY,
        last_quantity=last_quantity,
        cumulative_quantity=cumulative_quantity,
        fill_price=fill_price,
        fee=fee,
        fee_asset="USDT",
        occurred_at=datetime(2026, 7, 14, tzinfo=UTC),
        observation_source=FillObservationSource.SIMULATED_EXCHANGE,
        observation_reference=f"test:{trade_id}",
    )


def test_cancelled_intent_accepts_late_fill_and_exact_duplicate_idempotently(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    intent = _intent(client_order_id="late-fill-cancelled", plan_id="late-fill-plan")
    durable_intent_ledger.prepare(intent)
    durable_intent_ledger.mark_submitting(intent.client_order_id)
    durable_intent_ledger.record_fill(
        _fill(
            trade_id="late-fill-first",
            client_order_id=intent.client_order_id,
            last_quantity=Decimal("0.004"),
            cumulative_quantity=Decimal("0.004"),
            fill_price=Decimal("100"),
            fee=Decimal("0.001"),
        )
    )
    durable_intent_ledger.record_exchange_outcome(
        intent.client_order_id,
        status=DurableIntentStatus.CANCELLED,
        filled_quantity=Decimal("0.004"),
    )
    late_fill = _fill(
        trade_id="late-fill-second",
        client_order_id=intent.client_order_id,
        last_quantity=Decimal("0.006"),
        cumulative_quantity=Decimal("0.010"),
        fill_price=Decimal("102"),
        fee=Decimal("0.002"),
    )

    receipt = durable_intent_ledger.record_fill(late_fill)
    duplicate = durable_intent_ledger.record_fill(late_fill)
    rebuilt = durable_intent_ledger.reopen_after_restart().fill_ledger_for_plan(intent.plan_id)

    assert not receipt.is_duplicate
    assert duplicate.is_duplicate
    assert (
        durable_intent_ledger.intent(intent.client_order_id).status is DurableIntentStatus.CANCELLED
    )
    assert durable_intent_ledger.intent(intent.client_order_id).filled_quantity == Decimal("0.010")
    assert rebuilt.filled_quantity == Decimal("0.010")
    assert rebuilt.average_fill_price == Decimal("101.2")
    assert rebuilt.total_fee == Decimal("0.003")


def test_filled_intent_accepts_only_an_exact_duplicate_fill(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    intent = _intent(client_order_id="late-fill-filled", plan_id="late-fill-filled-plan")
    durable_intent_ledger.prepare(intent)
    durable_intent_ledger.mark_submitting(intent.client_order_id)
    fill = _fill(
        trade_id="filled-exact-duplicate",
        client_order_id=intent.client_order_id,
        last_quantity=Decimal("0.010"),
        cumulative_quantity=Decimal("0.010"),
        fill_price=Decimal("100"),
        fee=Decimal("0.001"),
    )
    durable_intent_ledger.record_fill(fill)
    durable_intent_ledger.record_exchange_outcome(
        intent.client_order_id,
        status=DurableIntentStatus.FILLED,
        filled_quantity=Decimal("0.010"),
    )

    duplicate = durable_intent_ledger.record_fill(fill)

    assert duplicate.is_duplicate
    assert duplicate.filled_quantity == Decimal("0.010")


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


def test_breaker_rejects_a_forged_repository_and_caller_supplied_outcome(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    class ForgedRepository:
        def collect_persistence_recovery_evidence(
            self,
            *,
            intent_ledger: DurableIntentLedger,
            reconciliation_outcome: ReconciliationOutcome,
        ) -> PersistenceRecoveryEvidence:
            return PersistenceRecoveryEvidence(
                write_probe_event_id="forged-probe",
                audit_event_count=1,
                audit_last_sequence=1,
                audit_last_record_hash="0" * 64,
                replay_valid=True,
                projection_matches_replay=True,
                reconciliation_outcome=reconciliation_outcome,
                unresolved_intent_ids=(),
            )

    breaker = PersistenceCircuitBreaker()
    with pytest.raises(TypeError):
        breaker.reset_after_verified_reconciliation(  # type: ignore[call-arg]
            audit_repository=ForgedRepository(),
            intent_ledger=durable_intent_ledger,
            reconciliation_outcome=_clean_outcome(),
        )


def test_breaker_derives_reconciliation_from_repository_ledger_and_algo_stop_records(
    session_factory: sessionmaker[Session],
) -> None:
    breaker = PersistenceCircuitBreaker()
    ledger = DurableIntentLedger(session_factory, persistence_breaker=breaker)
    repository = AuditRepository(session_factory, persistence_breaker=breaker)

    evidence = breaker.reset_after_verified_reconciliation(
        audit_repository=repository,
        intent_ledger=ledger,
        reconciliation_snapshot=exchange_reconciliation_batch(
            ReconciliationSnapshot(
                positions_by_symbol={},
                normal_order_client_ids=frozenset(),
                algo_order_client_ids=frozenset(),
            )
        ),
    )

    assert evidence.reconciliation_outcome.is_clean
    assert breaker.new_entries_allowed


def test_stop_protection_cannot_be_forged_by_a_symbol_boolean_without_an_algo_stop_record() -> None:
    with pytest.raises(ValueError, match="fresh exchange stop observations"):
        ReconciliationSnapshot(
            positions_by_symbol={"BTCUSDT": Decimal("0.005")},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
            stop_protected_symbols=frozenset({"BTCUSDT"}),
        )


def test_unknown_absence_requires_causal_query_evidence_and_rehydrates_after_restart(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "unknown-causal-plan"
    intent = _intent(client_order_id="unknown-causal-entry", plan_id=plan_id)
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(
            plan_id=plan_id,
            direction=Direction.LONG,
            worst_stop_exit_price=Decimal("90"),
            risk_budget=Decimal("1"),
        ),
        fault_plan=FaultPlan.from_faults((SimulatedFault.UNKNOWN_503,)),
        unknown_query_plan=SimulatedUnknownQueryPlan.from_states(
            (SimulatedUnknownRemoteState.ABSENT,)
        ),
        now_ms=10_000,
    )
    with pytest.raises(UnknownOrderOutcome):
        simulator.submit(intent)

    for observed_at_ms in (10_000, 11_000):
        simulator.advance_to(observed_at_ms)
        for source in AbsenceEvidenceSource:
            simulator.query_unknown_source(intent.client_order_id, source)

    restarted = ExchangeSimulator.reopen_after_restart(
        intent_ledger=durable_intent_ledger.reopen_after_restart(),
        now_ms=11_000,
    )
    restarted.resolve_unknown_as_absent(intent.client_order_id)

    assert durable_intent_ledger.intent(intent.client_order_id).status is DurableIntentStatus.ABSENT


def test_non_empty_audit_head_with_null_hash_is_invalid_for_replay(
    session_factory: sessionmaker[Session],
) -> None:
    repository = AuditRepository(session_factory)
    repository.record_delivery(
        event_id="null-head-hash",
        source="audit-test",
        event_type="checkpoint",
        occurred_at=datetime(2026, 7, 14, tzinfo=UTC),
        payload={"checkpoint": "third-audit"},
    )
    with session_factory.begin() as session:
        session.execute(
            text("UPDATE audit_chain_heads SET last_record_hash = NULL WHERE chain_id = 1")
        )

    replay = ReplayRunner().replay(repository.list_audit_events(), repository.audit_chain_head())

    assert not replay.is_valid
    assert replay.reason == "AUDIT_CHAIN_OR_HEAD_INVALID"


def test_durable_absence_models_reject_malformed_causal_evidence() -> None:
    with pytest.raises(ValueError, match="identify"):
        BoundedAbsenceEvidence(
            client_order_id="",
            economic_key="economic-key",
            first_not_found_at_ms=1,
            last_not_found_at_ms=2,
            not_found_observation_count=2,
        )
    with pytest.raises(ValueError, match="non-negative"):
        BoundedAbsenceEvidence(
            client_order_id="client-order",
            economic_key="economic-key",
            first_not_found_at_ms=-1,
            last_not_found_at_ms=2,
            not_found_observation_count=2,
        )
    with pytest.raises(ValueError, match="backward"):
        BoundedAbsenceEvidence(
            client_order_id="client-order",
            economic_key="economic-key",
            first_not_found_at_ms=2,
            last_not_found_at_ms=1,
            not_found_observation_count=2,
        )
    with pytest.raises(ValueError, match="non-negative"):
        UnknownIntentObservation(
            source=AbsenceEvidenceSource.TRADE_HISTORY,
            observed_at_ms=-1,
            stream_watermark_ms=0,
            found=False,
            query_reference="query-1",
            query_client_order_id="client-order",
            query_economic_key="economic-key",
            query_started_at_ms=0,
        )
    with pytest.raises(ValueError, match="watermark"):
        UnknownIntentObservation(
            source=AbsenceEvidenceSource.TRADE_HISTORY,
            observed_at_ms=2,
            stream_watermark_ms=1,
            found=False,
            query_reference="query-2",
            query_client_order_id="client-order",
            query_economic_key="economic-key",
            query_started_at_ms=1,
        )
    with pytest.raises(ValueError, match="query start"):
        UnknownIntentObservation(
            source=AbsenceEvidenceSource.TRADE_HISTORY,
            observed_at_ms=2,
            stream_watermark_ms=2,
            found=False,
            query_reference="query-3",
            query_client_order_id="client-order",
            query_economic_key="economic-key",
            query_started_at_ms=3,
        )
    with pytest.raises(ValueError, match="query identity"):
        UnknownIntentObservation(
            source=AbsenceEvidenceSource.TRADE_HISTORY,
            observed_at_ms=2,
            stream_watermark_ms=2,
            found=False,
            query_reference="",
            query_client_order_id="client-order",
            query_economic_key="economic-key",
            query_started_at_ms=1,
        )


def test_durable_policy_protection_and_fill_conflict_fail_closed(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "durable-policy-conflict"
    policy = _policy(
        plan_id=plan_id,
        direction=Direction.LONG,
        worst_stop_exit_price=Decimal("90"),
        risk_budget=Decimal("1"),
    )
    durable_intent_ledger.register_actual_risk_policy(policy)

    with pytest.raises(DurableRiskPolicyError, match="different durable"):
        durable_intent_ledger.register_actual_risk_policy(
            _policy(
                plan_id=plan_id,
                direction=Direction.LONG,
                worst_stop_exit_price=Decimal("90"),
                risk_budget=Decimal("2"),
            )
        )

    intent = _intent(client_order_id="durable-policy-fill", plan_id=plan_id)
    durable_intent_ledger.prepare(intent)
    durable_intent_ledger.mark_submitting(intent.client_order_id)
    fill = _fill(
        trade_id="durable-policy-trade",
        client_order_id=intent.client_order_id,
        last_quantity=Decimal("0.010"),
        cumulative_quantity=Decimal("0.010"),
        fill_price=Decimal("100"),
        fee=Decimal("0.001"),
    )
    durable_intent_ledger.record_fill(fill)

    with pytest.raises(FillLedgerError, match="semantic conflict"):
        durable_intent_ledger.record_fill(
            _fill(
                trade_id=fill.trade_id,
                client_order_id=intent.client_order_id,
                last_quantity=Decimal("0.010"),
                cumulative_quantity=Decimal("0.010"),
                fill_price=Decimal("100"),
                fee=Decimal("0.002"),
            )
        )
    with pytest.raises(DurableRiskPolicyError, match="must match"):
        durable_intent_ledger.record_simulated_protection(
            plan_id,
            protected_position_quantity=Decimal("0.009"),
        )

    risk = durable_intent_ledger.actual_risk_state(plan_id)

    assert risk.pending_entries_blocked
    assert risk.reason == "SIMULATED_PROTECTION_MISSING"


def test_durable_outcomes_reject_unproven_or_invalid_fill_quantities(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    intent = _intent(client_order_id="invalid-durable-outcome", plan_id="invalid-outcome-plan")
    durable_intent_ledger.prepare(intent)
    durable_intent_ledger.mark_submitting(intent.client_order_id)

    with pytest.raises(ValueError, match="known exchange outcome"):
        durable_intent_ledger.record_exchange_outcome(
            intent.client_order_id,
            status=DurableIntentStatus.UNKNOWN,
            filled_quantity=Decimal("0"),
        )
    with pytest.raises(ValueError, match="finite Decimal"):
        durable_intent_ledger.record_exchange_outcome(
            intent.client_order_id,
            status=DurableIntentStatus.NEW,
            filled_quantity=Decimal("NaN"),
        )
    with pytest.raises(IntentLifecycleError, match="durable fill facts"):
        durable_intent_ledger.record_exchange_outcome(
            intent.client_order_id,
            status=DurableIntentStatus.FILLED,
            filled_quantity=Decimal("0.010"),
        )


def test_absence_recording_rejects_missing_timestamps_before_persisting(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    intent = _intent(client_order_id="absence-missing-times", plan_id="absence-times-plan")
    durable_intent_ledger.prepare(intent)
    durable_intent_ledger.mark_submitting(intent.client_order_id)

    with pytest.raises(IntentLifecycleError, match="timestamps"):
        durable_intent_ledger.mark_unknown(intent.client_order_id)

    assert durable_intent_ledger.list_absence_observations(intent.client_order_id) == ()


@pytest.mark.postgresql
def test_postgresql_restart_rebuilds_durable_short_risk_and_blocks_follow_on_entry(
    postgresql_session_factory: sessionmaker[Session],
) -> None:
    breaker = PersistenceCircuitBreaker()
    ledger = DurableIntentLedger(postgresql_session_factory, persistence_breaker=breaker)
    repository = AuditRepository(postgresql_session_factory, persistence_breaker=breaker)
    breaker.reset_after_verified_reconciliation(
        audit_repository=repository,
        intent_ledger=ledger,
        reconciliation_snapshot=exchange_reconciliation_batch(
            ReconciliationSnapshot(
                positions_by_symbol={},
                normal_order_client_ids=frozenset(),
                algo_order_client_ids=frozenset(),
            )
        ),
    )
    plan_id = "postgresql-short-restart"
    simulator = ExchangeSimulator(
        intent_ledger=ledger,
        actual_risk_policy=_policy(
            plan_id=plan_id,
            direction=Direction.SHORT,
            worst_stop_exit_price=Decimal("101"),
            risk_budget=Decimal("0.02"),
        ),
        fill_plan=FillSequencePlan.from_sequences(
            (
                (
                    SimulatedFill(
                        trade_id="postgresql-short-fill",
                        last_quantity=Decimal("0.011"),
                        cumulative_quantity=Decimal("0.011"),
                        fill_price=Decimal("99"),
                        fee=Decimal("0"),
                        fee_asset="USDT",
                    ),
                ),
            )
        ),
    )
    simulator.submit(
        _intent(
            client_order_id="postgresql-short-entry-1",
            plan_id=plan_id,
            direction=Direction.SHORT,
            stage_index=1,
            quantity=Decimal("0.020"),
        )
    )

    restarted = ExchangeSimulator.reopen_after_restart(intent_ledger=ledger.reopen_after_restart())

    assert restarted.actual_risk(plan_id).pending_entries_blocked
    with pytest.raises(EntryRiskBlocked, match="ACTUAL_STOP_RISK_BREACH"):
        restarted.submit(
            _intent(
                client_order_id="postgresql-short-entry-2",
                plan_id=plan_id,
                direction=Direction.SHORT,
                stage_index=2,
            )
        )
