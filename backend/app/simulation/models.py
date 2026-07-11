"""Simulation event and intent value objects without a transport dependency."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.domain.decimal_math import ZERO
from app.domain.types import Direction


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
class SimulatedOrderIntent:
    client_order_id: str
    economic_key: str
    symbol: str
    direction: Direction
    role: OrderRole
    quantity: Decimal
    price: Decimal

    def __post_init__(self) -> None:
        if not self.client_order_id or not self.economic_key or not self.symbol:
            raise ValueError("client_order_id, economic_key, and symbol are required")
        if self.quantity <= ZERO or self.price <= ZERO:
            raise ValueError("simulated order quantity and price must be positive")


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
