"""Actual fill risk must drive simulator entry cancellation and future-entry blocking."""

from decimal import Decimal

import pytest

from app.domain.types import Direction
from app.simulation.intent_ledger import DurableIntentLedger
from app.simulation.models import (
    OrderRole,
    SimulatedFill,
    SimulatedOrderIntent,
    SimulatedOrderStatus,
)
from app.simulation.simulator import EntryRiskBlocked, ExchangeSimulator, FillSequencePlan
from tests.conftest import actual_risk_policy_for


def _intent(*, client_order_id: str, stage_index: int) -> SimulatedOrderIntent:
    return SimulatedOrderIntent(
        client_order_id=client_order_id,
        plan_id="actual-risk-plan",
        symbol="BTCUSDT",
        direction=Direction.LONG,
        role=OrderRole.ENTRY,
        stage_index=stage_index,
        quantity=Decimal("0.010"),
        price=Decimal("100"),
    )


def test_actual_fill_risk_cancels_partial_entry_and_blocks_remaining_stages(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=actual_risk_policy_for(
            "actual-risk-plan",
            worst_stop_exit_price=Decimal("90"),
            risk_budget=Decimal("0.1"),
        ),
        fill_plan=FillSequencePlan.from_sequences(
            (
                (
                    SimulatedFill(
                        trade_id="actual-risk-partial",
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

    first = simulator.submit(_intent(client_order_id="actual-risk-1", stage_index=1))

    assert first.status is SimulatedOrderStatus.CANCELLED
    assert simulator.actual_risk("actual-risk-plan").pending_entries_blocked
    with pytest.raises(EntryRiskBlocked, match="ACTUAL_STOP_RISK_BREACH"):
        simulator.submit(_intent(client_order_id="actual-risk-2", stage_index=2))
