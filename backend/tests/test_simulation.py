from decimal import Decimal

import pytest

from app.domain.types import Direction
from app.simulation.failure import FailureAction, FailureCoordinator
from app.simulation.models import (
    OrderRole,
    SimulatedFault,
    SimulatedOrderIntent,
    SimulatedOrderStatus,
)
from app.simulation.simulator import (
    DuplicateEconomicOrder,
    ExchangeSimulator,
    FaultPlan,
    InjectedExchangeError,
    SpreadSlippageModel,
    UnknownOrderOutcome,
)


def _intent(
    *, client_id: str = "UTA1-plan-EN-1-1", economic_key: str = "plan-1:ENTRY:1"
) -> SimulatedOrderIntent:
    return SimulatedOrderIntent(
        client_order_id=client_id,
        economic_key=economic_key,
        symbol="BTCUSDT",
        direction=Direction.LONG,
        role=OrderRole.ENTRY,
        quantity=Decimal("0.010"),
        price=Decimal("1000"),
    )


def test_partial_fill_and_duplicate_event_are_local_and_idempotency_visible() -> None:
    simulator = ExchangeSimulator(
        fault_plan=FaultPlan.from_faults(
            (SimulatedFault.PARTIAL_FILL, SimulatedFault.DUPLICATE_EVENT)
        )
    )
    partial = simulator.submit(_intent())
    duplicate = simulator.submit(_intent(client_id="UTA1-plan-TP-1-1", economic_key="plan-1:TP:1"))

    assert partial.status is SimulatedOrderStatus.PARTIALLY_FILLED
    assert partial.filled_quantity == Decimal("0.005")
    events = simulator.advance_to(0)
    assert len(events) == 3
    assert events[1].event_id == events[2].event_id
    assert duplicate.status is SimulatedOrderStatus.NEW


def test_delayed_event_and_unknown_outcome_prevent_duplicate_economic_order() -> None:
    delayed = ExchangeSimulator(fault_plan=FaultPlan.from_faults((SimulatedFault.DELAYED_EVENT,)))
    delayed.submit(_intent())
    assert delayed.advance_to(999) == ()
    assert len(delayed.advance_to(1_000)) == 1

    unknown = ExchangeSimulator(fault_plan=FaultPlan.from_faults((SimulatedFault.UNKNOWN_503,)))
    with pytest.raises(UnknownOrderOutcome):
        unknown.submit(_intent())
    with pytest.raises(DuplicateEconomicOrder):
        unknown.submit(_intent(client_id="UTA1-plan-EN-1-2"))
    unknown.resolve_unknown_as_absent("UTA1-plan-EN-1-1")
    replacement = unknown.submit(_intent(client_id="UTA1-plan-EN-1-2"))
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


def test_stop_rejection_and_transport_faults_are_injected_without_network() -> None:
    stop_intent = SimulatedOrderIntent(
        client_order_id="UTA1-plan-ST-1-1",
        economic_key="plan-1:STOP",
        symbol="BTCUSDT",
        direction=Direction.LONG,
        role=OrderRole.STOP,
        quantity=Decimal("0.010"),
        price=Decimal("990"),
    )
    simulator = ExchangeSimulator(fault_plan=FaultPlan.from_faults((SimulatedFault.REJECTED_STOP,)))
    with pytest.raises(InjectedExchangeError) as stop_error:
        simulator.submit(stop_intent)
    assert stop_error.value.fault is SimulatedFault.REJECTED_STOP
    assert simulator.order(stop_intent.client_order_id).status is SimulatedOrderStatus.REJECTED

    rate_limited = ExchangeSimulator(
        fault_plan=FaultPlan.from_faults((SimulatedFault.RATE_LIMIT_429,))
    )
    with pytest.raises(InjectedExchangeError) as rate_error:
        rate_limited.submit(_intent())
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
