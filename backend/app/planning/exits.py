"""Stop and take-profit planning from confirmed filled position quantity."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.domain.decimal_math import ZERO
from app.domain.filters import FilterViolation, SymbolFilters
from app.domain.types import Direction


class ExitPlanningError(ValueError):
    """Raised when a reduced-only exit cannot safely cover the confirmed position."""


@dataclass(frozen=True, slots=True)
class StopPlan:
    trigger_price: Decimal
    close_position: bool = True
    quantity: None = None


@dataclass(frozen=True, slots=True)
class TakeProfitLeg:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class ExitPlan:
    direction: Direction
    confirmed_position_quantity: Decimal
    stop: StopPlan
    take_profits: tuple[TakeProfitLeg, ...]
    cancel_pending_entries: bool = True


def build_exit_plan(
    *,
    direction: Direction,
    confirmed_position_quantity: Decimal,
    stop_price: Decimal,
    take_profit_prices: Sequence[Decimal],
    take_profit_weights: Sequence[Decimal],
    filters: SymbolFilters,
) -> ExitPlan:
    if confirmed_position_quantity <= ZERO or stop_price <= ZERO:
        raise ExitPlanningError("position quantity and stop price must be positive")
    if len(take_profit_prices) != len(take_profit_weights) or not take_profit_prices:
        raise ExitPlanningError("take-profit prices and weights must match")
    if any(weight <= ZERO for weight in take_profit_weights) or sum(
        take_profit_weights, ZERO
    ) != Decimal("1"):
        raise ExitPlanningError("take-profit weights must be positive and sum to one")
    if filters.round_quantity_down(confirmed_position_quantity) != confirmed_position_quantity:
        raise ExitPlanningError("confirmed position quantity must align to the exchange step")

    raw_quantities = [
        filters.round_quantity_down(confirmed_position_quantity * weight)
        for weight in take_profit_weights
    ]
    allocated_quantity = sum(raw_quantities, ZERO)
    remainder = confirmed_position_quantity - allocated_quantity
    raw_quantities[-1] += remainder

    take_profits: list[TakeProfitLeg] = []
    for price, quantity in zip(take_profit_prices, raw_quantities, strict=True):
        if direction is Direction.LONG and price <= stop_price:
            raise ExitPlanningError("long take-profit must remain above stop")
        if direction is Direction.SHORT and price >= stop_price:
            raise ExitPlanningError("short take-profit must remain below stop")
        try:
            filters.validate_entry(price=price, quantity=quantity)
        except FilterViolation as error:
            raise ExitPlanningError("take-profit violates an exchange filter") from error
        take_profits.append(TakeProfitLeg(price=price, quantity=quantity))

    if sum((leg.quantity for leg in take_profits), ZERO) > confirmed_position_quantity:
        raise ExitPlanningError("reduce-only take-profit quantity exceeds confirmed position")
    return ExitPlan(
        direction=direction,
        confirmed_position_quantity=confirmed_position_quantity,
        stop=StopPlan(trigger_price=stop_price),
        take_profits=tuple(take_profits),
    )
