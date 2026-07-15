from decimal import Decimal

import pytest
from conftest import actual_risk_policy_for

from app.domain.types import Direction
from app.simulation.failure import FailureAction, FailureCoordinator
from app.simulation.intent_ledger import (
    AbsenceEvidenceSource,
    DurableIntentLedger,
    UnknownIntentObservation,
    UnresolvedEconomicAction,
)
from app.simulation.models import (
    OrderRole,
    SimulatedFault,
    SimulatedOrderIntent,
    SimulatedOrderStatus,
)
from app.simulation.simulator import (
    ExchangeSimulator,
    FaultPlan,
    InjectedExchangeError,
    SpreadSlippageModel,
    UnknownOrderOutcome,
)


def _intent(
    *,
    client_id: str = "UTA1-plan-EN-1-1",
    plan_id: str = "plan-1",
    role: OrderRole = OrderRole.ENTRY,
    stage_index: int = 1,
) -> SimulatedOrderIntent:
    return SimulatedOrderIntent(
        client_order_id=client_id,
        plan_id=plan_id,
        symbol="BTCUSDT",
        direction=Direction.LONG,
        role=role,
        stage_index=stage_index,
        quantity=Decimal("0.010"),
        price=Decimal("1000"),
    )


def test_partial_fill_and_duplicate_event_are_local_and_idempotency_visible(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=actual_risk_policy_for("plan-1"),
        fault_plan=FaultPlan.from_faults(
            (SimulatedFault.PARTIAL_FILL, SimulatedFault.DUPLICATE_EVENT)
        ),
    )
    partial = simulator.submit(_intent())
    duplicate = simulator.submit(
        _intent(
            client_id="UTA1-plan-TP-1-1",
            role=OrderRole.TAKE_PROFIT,
        )
    )

    assert partial.status is SimulatedOrderStatus.PARTIALLY_FILLED
    assert partial.filled_quantity == Decimal("0.005")
    events = simulator.advance_to(0)
    assert len(events) == 3
    assert events[1].event_id == events[2].event_id
    assert events[0].trade_id is not None
    assert events[0].last_filled_quantity == Decimal("0.005")
    assert events[0].cumulative_filled_quantity == Decimal("0.005")
    assert events[0].fill_price == Decimal("1000")
    assert events[0].fee_asset == "USDT"
    assert events[0].occurred_at_ms == 0
    assert duplicate.status is SimulatedOrderStatus.NEW


def test_delayed_event_and_unknown_outcome_prevent_duplicate_economic_order(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    delayed = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=actual_risk_policy_for("plan-1"),
        fault_plan=FaultPlan.from_faults((SimulatedFault.DELAYED_EVENT,)),
    )
    delayed.submit(_intent())
    assert delayed.advance_to(999) == ()
    assert len(delayed.advance_to(1_000)) == 1

    unknown = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=actual_risk_policy_for("plan-1"),
        fault_plan=FaultPlan.from_faults((SimulatedFault.UNKNOWN_503,)),
    )
    unknown_intent = _intent(client_id="UTA1-plan-EN-2-1", stage_index=2)
    with pytest.raises(UnknownOrderOutcome):
        unknown.submit(unknown_intent)
    with pytest.raises(UnresolvedEconomicAction):
        unknown.submit(_intent(client_id="UTA1-plan-EN-2-2", stage_index=2))
    for source in AbsenceEvidenceSource:
        for observed_at_ms in (0, 1_000):
            unknown.record_unknown_absence_observation(
                unknown_intent.client_order_id,
                UnknownIntentObservation(
                    source=source,
                    observed_at_ms=observed_at_ms,
                    stream_watermark_ms=observed_at_ms + 1,
                    found=False,
                    query_reference=f"{source.value}-{observed_at_ms}",
                    query_client_order_id=unknown_intent.client_order_id,
                    query_economic_key=unknown_intent.economic_key,
                    query_started_at_ms=observed_at_ms,
                ),
            )
    unknown.resolve_unknown_as_absent(unknown_intent.client_order_id)
    replacement = unknown.submit(_intent(client_id="UTA1-plan-EN-2-2", stage_index=2))
    assert replacement.status is SimulatedOrderStatus.NEW


@pytest.mark.parametrize(
    ("fault", "expected_actions"),
    (
        (SimulatedFault.RATE_LIMIT_429, (FailureAction.PAUSE_NEW_ENTRIES,)),
        (SimulatedFault.IP_BAN_418, (FailureAction.HARD_HALT,)),
        (SimulatedFault.TIMESTAMP_1021, (FailureAction.RESYNC_CLOCK,)),
        (
            SimulatedFault.DISCONNECT,
            (FailureAction.PAUSE_NEW_ENTRIES, FailureAction.RECONCILE_REQUIRED),
        ),
        (
            SimulatedFault.REJECTED_STOP,
            (
                FailureAction.CANCEL_PENDING_ENTRIES,
                FailureAction.EMERGENCY_REDUCE,
                FailureAction.HARD_HALT,
            ),
        ),
    ),
)
def test_failure_coordinator_applies_safe_actions(
    fault: SimulatedFault, expected_actions: tuple[FailureAction, ...]
) -> None:
    coordinator = FailureCoordinator()

    assert coordinator.handle(fault) == expected_actions


def test_stop_rejection_and_transport_faults_are_injected_without_network(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    stop_intent = SimulatedOrderIntent(
        client_order_id="UTA1-plan-ST-1-1",
        plan_id="plan-1",
        symbol="BTCUSDT",
        direction=Direction.LONG,
        role=OrderRole.STOP,
        stage_index=1,
        quantity=Decimal("0.010"),
        price=Decimal("990"),
    )
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        fault_plan=FaultPlan.from_faults((SimulatedFault.REJECTED_STOP,)),
    )
    with pytest.raises(InjectedExchangeError) as stop_error:
        simulator.submit(stop_intent)
    assert stop_error.value.fault is SimulatedFault.REJECTED_STOP
    assert simulator.order(stop_intent.client_order_id).status is SimulatedOrderStatus.REJECTED

    rate_limited = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=actual_risk_policy_for("plan-1"),
        fault_plan=FaultPlan.from_faults((SimulatedFault.RATE_LIMIT_429,)),
    )
    with pytest.raises(InjectedExchangeError) as rate_error:
        rate_limited.submit(_intent(stage_index=2))
    assert rate_error.value.fault is SimulatedFault.RATE_LIMIT_429


def test_spread_slippage_model_is_directional_and_positive() -> None:
    model = SpreadSlippageModel(
        bid_price=Decimal("99"),
        ask_price=Decimal("101"),
        extra_slippage_bps=Decimal("10"),
    )

    assert model.spread_bps > Decimal("0")
    assert model.execution_price(Direction.LONG) > model.ask_price
    assert model.execution_price(Direction.SHORT) < model.bid_price
