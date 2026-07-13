"""Stop and reduce-only take-profit planning from confirmed exchange position quantity."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.domain.decimal_math import ZERO
from app.domain.filters import FilterViolation, SymbolFilters
from app.domain.types import Direction
from app.exchange.contracts import AlgoOrderType, NormalOrderType, OrderSide


class ExitPlanningError(ValueError):
    """Raised when a reduced-only exit cannot safely cover the confirmed position."""


@dataclass(frozen=True, slots=True)
class StopPlan:
    trigger_price: Decimal
    side: OrderSide
    algo_type: AlgoOrderType = AlgoOrderType.STOP_MARKET
    close_position: bool = True
    quantity: None = None

    def __post_init__(self) -> None:
        if self.algo_type is not AlgoOrderType.STOP_MARKET or not self.close_position:
            raise ExitPlanningError("protective stop must be a close-position STOP_MARKET")


@dataclass(frozen=True, slots=True)
class TakeProfitLeg:
    price: Decimal
    quantity: Decimal
    side: OrderSide
    reduce_only: bool = True
    order_type: NormalOrderType = NormalOrderType.REDUCE_ONLY_LIMIT

    def __post_init__(self) -> None:
        if not self.reduce_only or self.order_type is not NormalOrderType.REDUCE_ONLY_LIMIT:
            raise ExitPlanningError("take profits must be reduce-only LIMIT orders")


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
    rounded_stop_price = filters.round_stop_price(direction, stop_price)
    try:
        filters.validate_price(rounded_stop_price)
    except FilterViolation as error:
        raise ExitPlanningError("stop violates an exchange filter") from error

    rounded_prices = tuple(
        filters.round_take_profit_price(direction, price) for price in take_profit_prices
    )
    for price in rounded_prices:
        if direction is Direction.LONG and price <= rounded_stop_price:
            raise ExitPlanningError("long take-profit must remain above stop")
        if direction is Direction.SHORT and price >= rounded_stop_price:
            raise ExitPlanningError("short take-profit must remain below stop")
        try:
            filters.validate_price(price)
        except FilterViolation as error:
            raise ExitPlanningError("take-profit price violates an exchange filter") from error

    raw_quantities = [
        filters.round_quantity_down(confirmed_position_quantity * weight)
        for weight in take_profit_weights
    ]
    raw_quantities[-1] += confirmed_position_quantity - sum(raw_quantities, ZERO)
    exit_side = OrderSide.SELL if direction is Direction.LONG else OrderSide.BUY
    take_profits: list[TakeProfitLeg] = []
    dust_quantity = ZERO
    for price, quantity in zip(rounded_prices, raw_quantities, strict=True):
        candidate_quantity = quantity + dust_quantity
        try:
            filters.validate_quantity(candidate_quantity)
        except FilterViolation:
            dust_quantity = candidate_quantity
            continue
        if price * candidate_quantity < filters.min_notional:
            dust_quantity = candidate_quantity
            continue
        take_profits.append(
            TakeProfitLeg(
                price=price,
                quantity=candidate_quantity,
                side=exit_side,
            )
        )
        dust_quantity = ZERO

    if sum((leg.quantity for leg in take_profits), ZERO) > confirmed_position_quantity:
        raise ExitPlanningError("reduce-only take-profit quantity exceeds confirmed position")
    return ExitPlan(
        direction=direction,
        confirmed_position_quantity=confirmed_position_quantity,
        stop=StopPlan(trigger_price=rounded_stop_price, side=exit_side),
        take_profits=tuple(take_profits),
    )
