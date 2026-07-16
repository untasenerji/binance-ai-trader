"""Simulation event and intent value objects without a transport dependency."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.domain.decimal_math import ZERO
from app.domain.types import Direction

V1_DEFAULT_ACCOUNT_ID = "v1-primary"


class OrderRole(StrEnum):
    ENTRY = "ENTRY"
    STOP = "STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    EMERGENCY_REDUCE = "EMERGENCY_REDUCE"


class SimulatedOrderStatus(StrEnum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"


class SimulatedFault(StrEnum):
    PARTIAL_FILL = "PARTIAL_FILL"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    DELAYED_EVENT = "DELAYED_EVENT"
    REJECTED_STOP = "REJECTED_STOP"
    UNKNOWN_503 = "UNKNOWN_503"
    RATE_LIMIT_429 = "RATE_LIMIT_429"
    IP_BAN_418 = "IP_BAN_418"
    TIMESTAMP_1021 = "TIMESTAMP_1021"
    DISCONNECT = "DISCONNECT"


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    trade_id: str
    last_quantity: Decimal
    cumulative_quantity: Decimal
    fill_price: Decimal
    fee: Decimal
    fee_asset: str
    delivery_delay_ms: int = 0
    occurred_at_offset_ms: int = 0
    duplicate_delivery: bool = False

    def __post_init__(self) -> None:
        if not self.trade_id or not self.fee_asset:
            raise ValueError("simulated fills require a trade ID and fee asset")
        decimal_values = (
            self.last_quantity,
            self.cumulative_quantity,
            self.fill_price,
            self.fee,
        )
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in decimal_values):
            raise ValueError("simulated fill values must be finite Decimals")
        if self.last_quantity <= ZERO or self.cumulative_quantity < self.last_quantity:
            raise ValueError("simulated fill quantities are inconsistent")
        if self.fill_price <= ZERO or self.fee < ZERO:
            raise ValueError("simulated fill price or fee is invalid")
        integer_values = (self.delivery_delay_ms, self.occurred_at_offset_ms)
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in integer_values
        ):
            raise ValueError("simulated fill timing must use non-negative integer milliseconds")


@dataclass(frozen=True, slots=True)
class SimulatedOrderIntent:
    client_order_id: str
    plan_id: str
    symbol: str
    direction: Direction
    role: OrderRole
    stage_index: int
    quantity: Decimal
    price: Decimal
    account_id: str = V1_DEFAULT_ACCOUNT_ID

    def __post_init__(self) -> None:
        if not self.client_order_id or not self.plan_id or not self.symbol or not self.account_id:
            raise ValueError("account_id, client_order_id, plan_id, and symbol are required")
        if self.stage_index < 1:
            raise ValueError("stage_index must be positive")
        if (
            not self.quantity.is_finite()
            or not self.price.is_finite()
            or self.quantity <= ZERO
            or self.price <= ZERO
        ):
            raise ValueError("simulated order quantity and price must be positive")

    @property
    def economic_key(self) -> str:
        return ":".join(
            (
                self.plan_id,
                self.symbol,
                self.direction.value,
                self.role.value,
                str(self.stage_index),
            )
        )


@dataclass(slots=True)
class SimulatedOrder:
    intent: SimulatedOrderIntent
    status: SimulatedOrderStatus
    filled_quantity: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class SimulatorEvent:
    event_id: str
    client_order_id: str
    status: SimulatedOrderStatus
    filled_quantity: Decimal
    available_at_ms: int
    trade_id: str | None = None
    last_filled_quantity: Decimal = ZERO
    cumulative_filled_quantity: Decimal = ZERO
    fill_price: Decimal | None = None
    fee: Decimal = ZERO
    fee_asset: str | None = None
    occurred_at_ms: int = 0
