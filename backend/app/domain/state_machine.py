"""Explicit trade-plan transition rules and non-negotiable safety guards."""

from dataclasses import dataclass

from app.domain.types import TradePlanState


class TransitionRejected(ValueError):
    """Raised for a transition that could violate the trade safety lifecycle."""


@dataclass(frozen=True, slots=True)
class TransitionContext:
    risk_permits_entry: bool = False
    stop_confirmed: bool = False
    reduction_only: bool = False


_ALLOWED_TRANSITIONS: dict[TradePlanState, frozenset[TradePlanState]] = {
    TradePlanState.DRAFT: frozenset({TradePlanState.CANDIDATE, TradePlanState.CANCELLED}),
    TradePlanState.CANDIDATE: frozenset({TradePlanState.PLANNED, TradePlanState.CANCELLED}),
    TradePlanState.PLANNED: frozenset({TradePlanState.ARMED, TradePlanState.CANCELLED}),
    TradePlanState.ARMED: frozenset({TradePlanState.ENTRY_PENDING, TradePlanState.CANCELLED}),
    TradePlanState.ENTRY_PENDING: frozenset(
        {
            TradePlanState.PARTIALLY_FILLED,
            TradePlanState.POSITION_PROTECTED,
            TradePlanState.CANCELLED,
            TradePlanState.ORDER_STATUS_UNKNOWN,
            TradePlanState.HALTED,
        }
    ),
    TradePlanState.PARTIALLY_FILLED: frozenset(
        {
            TradePlanState.POSITION_PROTECTED,
            TradePlanState.EMERGENCY_REDUCE,
            TradePlanState.ORDER_STATUS_UNKNOWN,
            TradePlanState.HALTED,
        }
    ),
    TradePlanState.POSITION_PROTECTED: frozenset(
        {
            TradePlanState.MANAGING_POSITION,
            TradePlanState.ADD_PENDING,
            TradePlanState.TP_PARTIAL,
            TradePlanState.STOP_TRIGGERED,
            TradePlanState.HALTED,
        }
    ),
    TradePlanState.MANAGING_POSITION: frozenset(
        {
            TradePlanState.ADD_PENDING,
            TradePlanState.TP_PARTIAL,
            TradePlanState.STOP_TRIGGERED,
            TradePlanState.CLOSED,
            TradePlanState.HALTED,
        }
    ),
    TradePlanState.ADD_PENDING: frozenset(
        {
            TradePlanState.PARTIALLY_FILLED,
            TradePlanState.POSITION_PROTECTED,
            TradePlanState.MANAGING_POSITION,
            TradePlanState.CANCELLED,
            TradePlanState.HALTED,
        }
    ),
    TradePlanState.TP_PARTIAL: frozenset(
        {TradePlanState.MANAGING_POSITION, TradePlanState.CLOSED, TradePlanState.HALTED}
    ),
    TradePlanState.STOP_TRIGGERED: frozenset({TradePlanState.CLOSED, TradePlanState.HALTED}),
    TradePlanState.ORDER_STATUS_UNKNOWN: frozenset(
        {
            TradePlanState.ENTRY_PENDING,
            TradePlanState.PARTIALLY_FILLED,
            TradePlanState.POSITION_PROTECTED,
            TradePlanState.CANCELLED,
            TradePlanState.HALTED,
        }
    ),
    TradePlanState.EMERGENCY_REDUCE: frozenset({TradePlanState.CLOSED, TradePlanState.HALTED}),
    TradePlanState.CANCELLED: frozenset(),
    TradePlanState.CLOSED: frozenset(),
    TradePlanState.HALTED: frozenset(
        {TradePlanState.EMERGENCY_REDUCE, TradePlanState.CANCELLED, TradePlanState.CLOSED}
    ),
}


def transition(
    current: TradePlanState,
    target: TradePlanState,
    *,
    context: TransitionContext | None = None,
) -> TradePlanState:
    """Validate and return a permitted state change without performing an action."""
    active_context = context or TransitionContext()

    if target not in _ALLOWED_TRANSITIONS[current]:
        raise TransitionRejected(f"{current} cannot transition to {target}")

    if target is TradePlanState.ENTRY_PENDING and not active_context.risk_permits_entry:
        raise TransitionRejected("entry is blocked because risk does not permit it")

    if (
        target
        in {
            TradePlanState.POSITION_PROTECTED,
            TradePlanState.MANAGING_POSITION,
            TradePlanState.ADD_PENDING,
        }
        and not active_context.stop_confirmed
    ):
        raise TransitionRejected("position management requires a confirmed server-side stop")

    if current is TradePlanState.HALTED and not active_context.reduction_only:
        raise TransitionRejected("halted plans permit only risk-reducing recovery transitions")

    return target
