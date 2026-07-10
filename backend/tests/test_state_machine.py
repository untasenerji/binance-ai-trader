import pytest

from app.domain.state_machine import TransitionContext, TransitionRejected, transition
from app.domain.types import TradePlanState


def test_entry_requires_explicit_risk_permission() -> None:
    with pytest.raises(TransitionRejected, match="risk"):
        transition(TradePlanState.ARMED, TradePlanState.ENTRY_PENDING)


def test_position_management_requires_confirmed_stop() -> None:
    with pytest.raises(TransitionRejected, match="confirmed server-side stop"):
        transition(TradePlanState.PARTIALLY_FILLED, TradePlanState.POSITION_PROTECTED)


def test_valid_protected_lifecycle() -> None:
    assert (
        transition(
            TradePlanState.ARMED,
            TradePlanState.ENTRY_PENDING,
            context=TransitionContext(risk_permits_entry=True),
        )
        is TradePlanState.ENTRY_PENDING
    )
    assert (
        transition(
            TradePlanState.PARTIALLY_FILLED,
            TradePlanState.POSITION_PROTECTED,
            context=TransitionContext(stop_confirmed=True),
        )
        is TradePlanState.POSITION_PROTECTED
    )


def test_halted_plan_rejects_non_reducing_transition() -> None:
    with pytest.raises(TransitionRejected, match="risk-reducing"):
        transition(TradePlanState.HALTED, TradePlanState.CLOSED)

    assert (
        transition(
            TradePlanState.HALTED,
            TradePlanState.CLOSED,
            context=TransitionContext(reduction_only=True),
        )
        is TradePlanState.CLOSED
    )
