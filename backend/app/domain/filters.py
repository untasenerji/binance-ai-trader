"""Exchange-rule helpers that never infer precision from binary floats."""

from dataclasses import dataclass
from decimal import Decimal

from app.domain.decimal_math import ZERO, ceil_to_increment, floor_to_increment
from app.domain.types import Direction


class FilterViolation(ValueError):
    """Raised when a proposed price, quantity, or notional violates a symbol filter."""


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    """The subset of current exchange filters required by the planner."""

    symbol: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    min_notional: Decimal

    def __post_init__(self) -> None:
        if not self.symbol:
            raise FilterViolation("symbol is required")
        if self.tick_size <= ZERO or self.step_size <= ZERO:
            raise FilterViolation("tick_size and step_size must be positive")
        if self.min_price < ZERO or self.max_price < self.min_price:
            raise FilterViolation("price bounds are invalid")
        if self.min_quantity <= ZERO or self.max_quantity < self.min_quantity:
            raise FilterViolation("quantity bounds are invalid")
        if self.min_notional <= ZERO:
            raise FilterViolation("min_notional must be positive")

    def round_price_down(self, price: Decimal) -> Decimal:
        return floor_to_increment(price, self.tick_size, field="price")

    def round_price_up(self, price: Decimal) -> Decimal:
        return ceil_to_increment(price, self.tick_size, field="price")

    def round_entry_price(self, direction: Direction, price: Decimal) -> Decimal:
        return (
            self.round_price_up(price)
            if direction is Direction.LONG
            else self.round_price_down(price)
        )

    def round_stop_price(self, direction: Direction, price: Decimal) -> Decimal:
        return (
            self.round_price_down(price)
            if direction is Direction.LONG
            else self.round_price_up(price)
        )

    def round_take_profit_price(self, direction: Direction, price: Decimal) -> Decimal:
        return (
            self.round_price_down(price)
            if direction is Direction.LONG
            else self.round_price_up(price)
        )

    def round_quantity_down(self, quantity: Decimal) -> Decimal:
        return floor_to_increment(quantity, self.step_size, field="quantity")

    def validate_price(self, price: Decimal) -> None:
        if price < self.min_price or price > self.max_price:
            raise FilterViolation("price is outside exchange bounds")
        if self.round_price_down(price) != price:
            raise FilterViolation("price is not aligned to tick_size")

    def validate_quantity(self, quantity: Decimal) -> None:
        if quantity < self.min_quantity or quantity > self.max_quantity:
            raise FilterViolation("quantity is outside exchange bounds")
        if self.round_quantity_down(quantity) != quantity:
            raise FilterViolation("quantity is not aligned to step_size")

    def validate_entry(self, *, price: Decimal, quantity: Decimal) -> Decimal:
        """Validate a complete entry and return its exact notional."""
        self.validate_price(price)
        self.validate_quantity(quantity)
        notional = price * quantity
        if notional < self.min_notional:
            raise FilterViolation("notional is below exchange minimum")
        return notional
