from datetime import UTC, datetime
from decimal import Decimal

import pytest
from conftest import actual_risk_policy_for

from app.domain.types import Direction
from app.exchange.contracts import (
    LocalReconciliationState,
    ReconciliationSnapshot,
    reconcile_local_state,
)
from app.planning.fills import FillEvent, FillLedger
from app.simulation.failure import FailureCoordinator
from app.simulation.intent_ledger import DurableIntentLedger, DurableIntentStatus
from app.simulation.models import (
    OrderRole,
    SimulatedFault,
    SimulatedFill,
    SimulatedOrderIntent,
    SimulatedOrderStatus,
    SimulatorEvent,
)
from app.simulation.simulator import (
    ExchangeSimulator,
    FaultPlan,
    FillSequencePlan,
    UnknownOrderOutcome,
)


def _intent(client_order_id: str = "matrix-entry") -> SimulatedOrderIntent:
    return SimulatedOrderIntent(
        client_order_id=client_order_id,
        plan_id="matrix-plan",
        symbol="BTCUSDT",
        direction=Direction.LONG,
        role=OrderRole.ENTRY,
        stage_index=1,
        quantity=Decimal("0.010"),
        price=Decimal("100"),
    )


def _as_fill(event_trade_id: str, event: SimulatorEvent) -> FillEvent:
    if event.fill_price is None or event.fee_asset is None:
        raise AssertionError("simulated trade event must carry price and fee asset")
    return FillEvent(
        trade_id=event_trade_id,
        client_order_id=event.client_order_id,
        last_quantity=event.last_filled_quantity,
        cumulative_quantity=event.cumulative_filled_quantity,
        fill_price=event.fill_price,
        fee=event.fee,
        fee_asset=event.fee_asset,
        occurred_at=datetime(2026, 7, 12, tzinfo=UTC),
    )


def test_configurable_fill_sequence_keeps_delta_cumulative_financial_invariants(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    fills = (
        SimulatedFill(
            trade_id="trade-first",
            last_quantity=Decimal("0.003"),
            cumulative_quantity=Decimal("0.003"),
            fill_price=Decimal("100"),
            fee=Decimal("0.001"),
            fee_asset="USDT",
            delivery_delay_ms=100,
        ),
        SimulatedFill(
            trade_id="trade-second",
            last_quantity=Decimal("0.007"),
            cumulative_quantity=Decimal("0.010"),
            fill_price=Decimal("101"),
            fee=Decimal("0.002"),
            fee_asset="USDT",
            delivery_delay_ms=0,
            duplicate_delivery=True,
        ),
    )
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=actual_risk_policy_for("matrix-plan"),
        fill_plan=FillSequencePlan.from_sequences((fills,)),
    )

    order = simulator.submit(_intent())
    events = simulator.advance_to(100)
    ledger = FillLedger()
    delivered_fills = tuple(event for event in events if event.trade_id is not None)
    # User-stream delivery may be delayed/out of order; reconciliation consumes the
    # complete durable facts in cumulative order and must still reject a real gap.
    receipts = []
    for event in sorted(
        delivered_fills,
        key=lambda event: (event.cumulative_filled_quantity, event.trade_id or ""),
    ):
        assert event.trade_id is not None
        receipts.append(ledger.record(_as_fill(event.trade_id, event)))

    assert order.status is SimulatedOrderStatus.FILLED
    assert order.filled_quantity == Decimal("0.010")
    assert [event.trade_id for event in events] == ["trade-second", "trade-second", "trade-first"]
    assert receipts[2].is_duplicate
    assert ledger.filled_quantity == Decimal("0.010")
    assert ledger.average_fill_price == Decimal("100.7")
    assert ledger.total_fee == Decimal("0.003")
    assert durable_intent_ledger.intent("matrix-entry").status is DurableIntentStatus.FILLED
    assert durable_intent_ledger.fill_ledger_for_plan("matrix-plan").average_fill_price == Decimal(
        "100.7"
    )


def test_partial_fill_cancel_keeps_known_partial_quantity_across_restart_boundary(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=actual_risk_policy_for("matrix-plan"),
        fill_plan=FillSequencePlan.from_sequences(
            (
                (
                    SimulatedFill(
                        trade_id="partial-trade",
                        last_quantity=Decimal("0.005"),
                        cumulative_quantity=Decimal("0.005"),
                        fill_price=Decimal("100"),
                        fee=Decimal("0.001"),
                        fee_asset="USDT",
                    ),
                ),
            )
        ),
    )
    order = simulator.submit(_intent("partial-entry"))
    assert order.status is SimulatedOrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == Decimal("0.005")
    restarted_ledger = durable_intent_ledger.reopen_after_restart()
    assert restarted_ledger.intent("partial-entry").status is DurableIntentStatus.PARTIALLY_FILLED
    assert restarted_ledger.intent("partial-entry").filled_quantity == Decimal("0.005")
    cancelled = simulator.cancel("partial-entry")

    assert cancelled.status is SimulatedOrderStatus.CANCELLED
    assert cancelled.filled_quantity == Decimal("0.005")
    assert durable_intent_ledger.intent("partial-entry").status is DurableIntentStatus.CANCELLED
    assert durable_intent_ledger.intent("partial-entry").filled_quantity == Decimal("0.005")


def test_unknown_failure_remains_paused_until_a_clean_typed_reconciliation_outcome(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=actual_risk_policy_for("matrix-plan"),
        fault_plan=FaultPlan.from_faults((SimulatedFault.UNKNOWN_503,)),
    )
    coordinator = FailureCoordinator()
    with pytest.raises(UnknownOrderOutcome):
        simulator.submit(_intent("unknown-entry"))
    coordinator.handle(SimulatedFault.UNKNOWN_503)
    dirty = reconcile_local_state(
        local=LocalReconciliationState(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
            required_stop_symbols=frozenset(),
            unresolved_unknown_intent_ids=frozenset({"unknown-entry"}),
            audit_chain_valid=True,
            replay_valid=True,
        ),
        snapshot=ReconciliationSnapshot(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
        ),
    )

    assert not coordinator.mark_reconciled(dirty)
    assert coordinator.new_entries_paused

    simulator.reconcile_unknown(
        "unknown-entry",
        status=SimulatedOrderStatus.CANCELLED,
        filled_quantity=Decimal("0"),
    )
    clean = reconcile_local_state(
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

    assert coordinator.mark_reconciled(clean)
    assert not coordinator.new_entries_paused
