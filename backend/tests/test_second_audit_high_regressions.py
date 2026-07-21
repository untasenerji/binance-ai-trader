"""Regression cases from the second independent audit; each begins as fail-closed evidence."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.domain.filters import SymbolFilters
from app.domain.risk import RiskSettings
from app.domain.types import Direction
from app.exchange.contracts import ReconciliationOutcome
from app.persistence.audit import AuditRepository
from app.persistence.circuit_breaker import (
    PersistenceCircuitBreaker,
    PersistenceRecoveryEvidence,
)
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.persistence.replay import ReplayRunner
from app.planning.fills import (
    FillEvent,
    FillLedger,
    FillLedgerError,
    FillObservationSource,
    FillSide,
)
from app.planning.ladder import StageBlueprint
from app.planning.risk import CostAssumptions, PlanningContext, solve_ladder
from app.simulation.intent_ledger import (
    AbsenceEvidenceSource,
    BoundedAbsenceEvidence,
    BoundedAbsenceEvidenceError,
    DurableIntentLedger,
    DurableIntentStatus,
)
from app.simulation.models import OrderRole, SimulatedOrderIntent
from app.simulation.simulator import (
    ExchangeSimulator,
    SimulatedUnknownQueryPlan,
    SimulatedUnknownRemoteState,
)
from tests.strategy_factory import make_strategy_lineage


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'second-audit-high.sqlite'}")
    create_schema(engine)
    return create_session_factory(engine)


def _settings() -> RiskSettings:
    return RiskSettings(
        pilot_equity_cap_usdt=Decimal("20"),
        max_leverage=2,
        max_concurrent_positions=1,
        max_active_strategy_count=1,
        max_stages=2,
        risk_per_trade_usdt=Decimal("0.10"),
        daily_loss_limit_usdt=Decimal("0.30"),
        weekly_drawdown_limit_usdt=Decimal("0.80"),
        consecutive_loss_limit=3,
        openai_daily_budget_usd=Decimal("0.02"),
    )


def _context(**overrides: object) -> PlanningContext:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "requested_settings": _settings(),
        "verified_available_equity_usdt": Decimal("20"),
        "verified_effective_leverage": 2,
        "leverage_bracket_notional_cap_usdt": Decimal("40"),
        "required_reserve_usdt": Decimal("0"),
        "existing_symbol_exposure_usdt": Decimal("0"),
        "existing_total_exposure_usdt": Decimal("0"),
        "symbol_exposure_cap_usdt": Decimal("40"),
        "total_exposure_cap_usdt": Decimal("40"),
        "existing_open_position_count": 0,
        "active_strategy_count": 0,
        "remaining_daily_loss_usdt": Decimal("0.30"),
        "remaining_weekly_drawdown_usdt": Decimal("0.80"),
        "consecutive_loss_count": 0,
        "isolated_margin_verified": True,
        "one_way_mode_verified": True,
        "server_stop_capable": True,
    }
    values.update(overrides)
    return PlanningContext(**values)  # type: ignore[arg-type]


def _filters() -> SymbolFilters:
    return SymbolFilters(
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        min_price=Decimal("1"),
        max_price=Decimal("1000000"),
        step_size=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("1000"),
        min_notional=Decimal("1"),
    )


def _short_costs() -> CostAssumptions:
    return CostAssumptions(
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        entry_slippage_bps=Decimal("100"),
        stop_slippage_bps=Decimal("0"),
        funding_buffer_rate=Decimal("0"),
        funding_interval_count=0,
    )


def test_short_plan_uses_order_limit_not_adverse_loss_fill_for_exposure_and_margin() -> None:
    plan = solve_ladder(
        strategy_lineage=make_strategy_lineage("second-audit-short"),
        direction=Direction.SHORT,
        blueprints=(StageBlueprint(index=1, entry_price=Decimal("100"), weight=Decimal("1")),),
        stop_price=Decimal("100.1"),
        filters=_filters(),
        costs=_short_costs(),
        planning_context=_context(),
    )

    assert not plan.is_skipped
    expected_notional = sum(
        (stage.quantity * stage.entry_price for stage in plan.stages), Decimal("0")
    )
    assert plan.planned_notional_usdt == expected_notional
    assert plan.required_margin_usdt == expected_notional / Decimal("2")


def test_short_limit_exposure_cannot_pass_using_a_lower_adverse_loss_fill_price() -> None:
    plan = solve_ladder(
        strategy_lineage=make_strategy_lineage("second-audit-short"),
        direction=Direction.SHORT,
        blueprints=(StageBlueprint(index=1, entry_price=Decimal("100"), weight=Decimal("1")),),
        stop_price=Decimal("100.1"),
        filters=_filters(),
        costs=_short_costs(),
        planning_context=_context(
            leverage_bracket_notional_cap_usdt=Decimal("8.95"),
            symbol_exposure_cap_usdt=Decimal("8.95"),
            total_exposure_cap_usdt=Decimal("8.95"),
        ),
    )

    assert plan.is_skipped
    assert plan.skip_reason == "EXPOSURE_LIMIT_EXCEEDED"


def _dirty_outcome_without_reason_codes() -> ReconciliationOutcome:
    return ReconciliationOutcome(
        missing_normal_order_ids=("normal-missing",),
        unexpected_normal_order_ids=(),
        missing_algo_order_ids=(),
        unexpected_algo_order_ids=(),
        position_quantity_mismatches=(),
        missing_expected_positions=(),
        unexpected_exchange_positions=(),
        missing_stop_symbols=(),
        unresolved_unknown_intent_ids=(),
        audit_chain_valid=True,
        replay_valid=True,
    )


def test_reconciliation_cleanliness_is_derived_from_all_content_not_only_reason_codes() -> None:
    assert not _dirty_outcome_without_reason_codes().is_clean


def test_breaker_rejects_caller_forged_booleans_and_counts() -> None:
    breaker = PersistenceCircuitBreaker()
    breaker.record_write_failure(OSError("injected persistence failure"))
    with pytest.raises(TypeError):
        PersistenceRecoveryEvidence(  # type: ignore[call-arg]
            durable_write_probe_succeeded=True,
            audit_chain_valid=True,
            replay_valid=True,
            unresolved_unknown_count=0,
        )
    with pytest.raises(TypeError):
        breaker.reset_after_verified_reconciliation(  # type: ignore[misc, call-arg]
            "caller-supplied evidence"
        )

    assert not breaker.new_entries_allowed


def _intent() -> SimulatedOrderIntent:
    return SimulatedOrderIntent(
        client_order_id="audit-high-intent",
        plan_id="audit-high-plan",
        symbol="BTCUSDT",
        direction=Direction.LONG,
        role=OrderRole.ENTRY,
        stage_index=1,
        quantity=Decimal("0.010"),
        price=Decimal("100"),
    )


def test_unknown_intent_cannot_resolve_from_caller_supplied_timestamps_and_count(
    session_factory: sessionmaker[Session],
) -> None:
    ledger = DurableIntentLedger(session_factory, persistence_breaker=PersistenceCircuitBreaker())
    # This direct lifecycle setup bypasses entry authorization to isolate evidence handling.
    with session_factory.begin() as session:
        from app.persistence.models import DurableOrderIntent

        session.add(
            DurableOrderIntent(
                economic_key=_intent().economic_key,
                attempt_number=1,
                client_order_id=_intent().client_order_id,
                plan_id=_intent().plan_id,
                symbol=_intent().symbol,
                direction=_intent().direction.value,
                role=_intent().role.value,
                stage_index=_intent().stage_index,
                quantity="0.010",
                price="100",
                filled_quantity="0",
                status=DurableIntentStatus.UNKNOWN.value,
                submitted_at_ms=0,
                unknown_at_ms=0,
            )
        )
    supplied = BoundedAbsenceEvidence(
        client_order_id=_intent().client_order_id,
        economic_key=_intent().economic_key,
        first_not_found_at_ms=0,
        last_not_found_at_ms=1_000,
        not_found_observation_count=2,
    )

    with pytest.raises(BoundedAbsenceEvidenceError):
        ledger.resolve_unknown_as_absent(_intent().client_order_id, supplied)


def _fill(
    *,
    trade_id: str,
    client_order_id: str,
    last_quantity: Decimal,
    cumulative_quantity: Decimal,
    price: Decimal,
) -> FillEvent:
    return FillEvent(
        account_id="v1-primary",
        trade_id=trade_id,
        client_order_id=client_order_id,
        symbol="BTCUSDT",
        side=FillSide.BUY,
        last_quantity=last_quantity,
        cumulative_quantity=cumulative_quantity,
        fill_price=price,
        fee=Decimal("0.001"),
        fee_asset="USDT",
        occurred_at=datetime(2026, 7, 13, tzinfo=UTC),
        observation_source=FillObservationSource.SIMULATED_EXCHANGE,
        observation_reference=f"test:{trade_id}",
    )


def test_fill_ledger_aggregates_multiple_entry_stages_into_one_exact_vwap() -> None:
    ledger = FillLedger()
    ledger.record(
        _fill(
            trade_id="stage-one",
            client_order_id="entry-stage-1",
            last_quantity=Decimal("0.002"),
            cumulative_quantity=Decimal("0.002"),
            price=Decimal("100"),
        )
    )
    ledger.record(
        _fill(
            trade_id="stage-two",
            client_order_id="entry-stage-2",
            last_quantity=Decimal("0.003"),
            cumulative_quantity=Decimal("0.003"),
            price=Decimal("102"),
        )
    )

    assert ledger.filled_quantity == Decimal("0.005")
    assert ledger.average_fill_price == Decimal("101.2")


def test_fill_ledger_rejects_semantic_conflict_for_reused_trade_id() -> None:
    ledger = FillLedger()
    ledger.record(
        _fill(
            trade_id="same-trade",
            client_order_id="entry-stage-1",
            last_quantity=Decimal("0.002"),
            cumulative_quantity=Decimal("0.002"),
            price=Decimal("100"),
        )
    )

    with pytest.raises(FillLedgerError, match="semantic"):
        ledger.record(
            _fill(
                trade_id="same-trade",
                client_order_id="entry-stage-1",
                last_quantity=Decimal("0.002"),
                cumulative_quantity=Decimal("0.002"),
                price=Decimal("101"),
            )
        )


def test_fill_ledger_rejects_a_cumulative_gap_before_risk_can_be_declared_safe() -> None:
    ledger = FillLedger()

    with pytest.raises(FillLedgerError, match="cumulative"):
        ledger.record(
            _fill(
                trade_id="gap-trade",
                client_order_id="entry-stage-1",
                last_quantity=Decimal("0.003"),
                cumulative_quantity=Decimal("0.010"),
                price=Decimal("100"),
            )
        )


def test_durable_intent_permits_partial_to_filled_progression(
    session_factory: sessionmaker[Session],
) -> None:
    breaker = PersistenceCircuitBreaker()
    ledger = DurableIntentLedger(session_factory, persistence_breaker=breaker)
    intent = _intent()
    with session_factory.begin() as session:
        from app.persistence.models import DurableOrderIntent

        session.add(
            DurableOrderIntent(
                economic_key=intent.economic_key,
                attempt_number=1,
                client_order_id=intent.client_order_id,
                plan_id=intent.plan_id,
                symbol=intent.symbol,
                direction=intent.direction.value,
                role=intent.role.value,
                stage_index=intent.stage_index,
                quantity="0.010",
                price="100",
                filled_quantity="0",
                status=DurableIntentStatus.SUBMITTING.value,
            )
        )
    ledger.record_fill(
        _fill(
            trade_id="partial-trade",
            client_order_id=intent.client_order_id,
            last_quantity=Decimal("0.005"),
            cumulative_quantity=Decimal("0.005"),
            price=Decimal("100"),
        )
    )
    ledger.record_exchange_outcome(
        intent.client_order_id,
        status=DurableIntentStatus.PARTIALLY_FILLED,
        filled_quantity=Decimal("0.005"),
    )
    ledger.record_fill(
        _fill(
            trade_id="filled-trade",
            client_order_id=intent.client_order_id,
            last_quantity=Decimal("0.005"),
            cumulative_quantity=Decimal("0.010"),
            price=Decimal("102"),
        )
    )

    completed = ledger.record_exchange_outcome(
        intent.client_order_id,
        status=DurableIntentStatus.FILLED,
        filled_quantity=Decimal("0.010"),
    )

    assert completed.status is DurableIntentStatus.FILLED


def test_durable_fill_facts_rebuild_multistage_vwap_and_fees_after_restart(
    session_factory: sessionmaker[Session],
) -> None:
    ledger = DurableIntentLedger(session_factory, persistence_breaker=PersistenceCircuitBreaker())
    first = _intent()
    second = SimulatedOrderIntent(
        client_order_id="audit-high-intent-stage-2",
        plan_id=first.plan_id,
        symbol=first.symbol,
        direction=first.direction,
        role=first.role,
        stage_index=2,
        quantity=Decimal("0.003"),
        price=Decimal("102"),
    )
    with session_factory.begin() as session:
        from app.persistence.models import DurableOrderIntent

        for intent in (first, second):
            session.add(
                DurableOrderIntent(
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
    ledger.record_fill(
        _fill(
            trade_id="restart-stage-1",
            client_order_id=first.client_order_id,
            last_quantity=Decimal("0.002"),
            cumulative_quantity=Decimal("0.002"),
            price=Decimal("100"),
        )
    )
    ledger.record_fill(
        _fill(
            trade_id="restart-stage-2",
            client_order_id=second.client_order_id,
            last_quantity=Decimal("0.003"),
            cumulative_quantity=Decimal("0.003"),
            price=Decimal("102"),
        )
    )

    restarted = ledger.reopen_after_restart()
    rebuilt = restarted.fill_ledger_for_plan(first.plan_id)

    assert rebuilt.filled_quantity == Decimal("0.005")
    assert rebuilt.average_fill_price == Decimal("101.2")
    assert rebuilt.total_fee == Decimal("0.002")


def test_unknown_absence_needs_durable_repeated_observations_from_every_source(
    session_factory: sessionmaker[Session],
) -> None:
    ledger = DurableIntentLedger(session_factory, persistence_breaker=PersistenceCircuitBreaker())
    intent = _intent()
    with session_factory.begin() as session:
        from app.persistence.models import DurableOrderIntent

        session.add(
            DurableOrderIntent(
                economic_key=intent.economic_key,
                attempt_number=1,
                client_order_id=intent.client_order_id,
                plan_id=intent.plan_id,
                symbol=intent.symbol,
                direction=intent.direction.value,
                role=intent.role.value,
                stage_index=intent.stage_index,
                quantity="0.010",
                price="100",
                filled_quantity="0",
                status=DurableIntentStatus.UNKNOWN.value,
                submitted_at_ms=0,
                unknown_at_ms=0,
            )
        )
    simulator = ExchangeSimulator.reopen_after_restart(
        intent_ledger=ledger,
        unknown_query_plan=SimulatedUnknownQueryPlan.from_states(
            (SimulatedUnknownRemoteState.ABSENT,)
        ),
    )
    for observed_at_ms in (0, 1_000):
        simulator.advance_to(observed_at_ms)
        for source in AbsenceEvidenceSource:
            simulator.query_unknown_source(intent.client_order_id, source)

    evidence = ledger.bounded_absence_evidence(intent.client_order_id)
    resolved = ledger.reopen_after_restart().resolve_unknown_as_absent(intent.client_order_id)

    assert evidence.is_sufficient
    assert resolved.status is DurableIntentStatus.ABSENT


def test_tampering_delivery_metadata_or_head_sequence_breaks_replay(
    session_factory: sessionmaker[Session],
) -> None:
    repository = AuditRepository(session_factory)
    repository.record_delivery(
        event_id="metadata-protected",
        source="strategy",
        event_type="state_transition",
        occurred_at=datetime(2026, 7, 13, tzinfo=UTC),
        payload={
            "plan_id": "metadata-plan",
            "from_state": "DRAFT",
            "to_state": "CANDIDATE",
            "plan_version": 1,
            "source_sequence": 1,
            "transition_evidence": {
                "risk_permits_entry": False,
                "stop_confirmed": False,
                "reduction_only": False,
            },
        },
    )
    with session_factory.begin() as session:
        session.execute(text("DROP TRIGGER prevent_audit_events_update"))
        session.execute(
            text(
                "UPDATE audit_events SET delivery_status = 'EXACT_DUPLICATE', "
                "semantic_fingerprint = '0' WHERE event_id = 'metadata-protected'"
            )
        )

    assert (
        not ReplayRunner()
        .replay(repository.list_audit_events(), repository.audit_chain_head())
        .is_valid
    )

    repository = AuditRepository(session_factory)
    with session_factory.begin() as session:
        session.execute(text("UPDATE audit_chain_heads SET last_sequence = 999 WHERE chain_id = 1"))

    assert (
        not ReplayRunner()
        .replay(repository.list_audit_events(), repository.audit_chain_head())
        .is_valid
    )
