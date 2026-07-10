"""Core trade-plan value objects without exchange or credential dependencies."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.domain.decimal_math import ZERO


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class TradePlanState(StrEnum):
    DRAFT = "DRAFT"
    CANDIDATE = "CANDIDATE"
    PLANNED = "PLANNED"
    ARMED = "ARMED"
    ENTRY_PENDING = "ENTRY_PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    POSITION_PROTECTED = "POSITION_PROTECTED"
    MANAGING_POSITION = "MANAGING_POSITION"
    ADD_PENDING = "ADD_PENDING"
    TP_PARTIAL = "TP_PARTIAL"
    STOP_TRIGGERED = "STOP_TRIGGERED"
    ORDER_STATUS_UNKNOWN = "ORDER_STATUS_UNKNOWN"
    EMERGENCY_REDUCE = "EMERGENCY_REDUCE"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"
    HALTED = "HALTED"


@dataclass(frozen=True, slots=True)
class TradeStage:
    """A pre-planned ladder stage; quantities are exact Decimal values."""

    stage_index: int
    entry_price: Decimal
    quantity: Decimal
    filled_quantity: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.stage_index < 1:
            raise ValueError("stage_index must start at 1")
        if self.entry_price <= ZERO:
            raise ValueError("entry_price must be positive")
        if self.quantity <= ZERO:
            raise ValueError("quantity must be positive")
        if self.filled_quantity < ZERO or self.filled_quantity > self.quantity:
            raise ValueError("filled_quantity must be within the planned quantity")

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity
