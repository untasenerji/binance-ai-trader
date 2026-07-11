"""Deterministic local exchange behavior used to prove failure safety paths."""

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from app.domain.decimal_math import ZERO
from app.domain.types import Direction
from app.simulation.models import (
    OrderRole,
    SimulatedFault,
    SimulatedOrder,
    SimulatedOrderIntent,
    SimulatedOrderStatus,
    SimulatorEvent,
)

_BPS_DENOMINATOR = Decimal("10000")


class SimulatorError(RuntimeError):
    code = "SIMULATOR_ERROR"


class DuplicateEconomicOrder(SimulatorError):
    code = "DUPLICATE_ECONOMIC_ORDER"


class UnknownOrderOutcome(SimulatorError):
    code = "ORDER_STATUS_UNKNOWN"


class InjectedExchangeError(SimulatorError):
    def __init__(self, fault: SimulatedFault) -> None:
        self.fault = fault
        super().__init__(fault.value)


@dataclass(slots=True)
class FaultPlan:
    faults: deque[SimulatedFault] = field(default_factory=deque)

    @classmethod
    def from_faults(cls, faults: Iterable[SimulatedFault]) -> "FaultPlan":
        return cls(faults=deque(faults))

    def consume(self) -> SimulatedFault | None:
        return self.faults.popleft() if self.faults else None


@dataclass(frozen=True, slots=True)
class SpreadSlippageModel:
    bid_price: Decimal
    ask_price: Decimal
    extra_slippage_bps: Decimal = ZERO

    def __post_init__(self) -> None:
        if (
            self.bid_price <= ZERO
            or self.ask_price < self.bid_price
            or self.extra_slippage_bps < ZERO
        ):
            raise ValueError("spread and slippage inputs are invalid")

    @property
    def midpoint(self) -> Decimal:
        return (self.bid_price + self.ask_price) / Decimal("2")

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask_price - self.bid_price) * _BPS_DENOMINATOR / self.midpoint

    def execution_price(self, direction: Direction) -> Decimal:
        slippage_rate = self.extra_slippage_bps / _BPS_DENOMINATOR
        base_price = self.ask_price if direction is Direction.LONG else self.bid_price
        return (
            base_price * (Decimal("1") + slippage_rate)
            if direction is Direction.LONG
            else base_price * (Decimal("1") - slippage_rate)
        )


@dataclass(slots=True)
class ExchangeSimulator:
    fault_plan: FaultPlan = field(default_factory=FaultPlan)
    now_ms: int = 0
    _orders: dict[str, SimulatedOrder] = field(default_factory=dict)
    _economic_keys: dict[str, str] = field(default_factory=dict)
    _events: list[SimulatorEvent] = field(default_factory=list)
    _event_counter: int = 0

    def submit(self, intent: SimulatedOrderIntent) -> SimulatedOrder:
        if intent.client_order_id in self._orders or intent.economic_key in self._economic_keys:
            raise DuplicateEconomicOrder("client ID or economic intent already exists")

        fault = self.fault_plan.consume()
        if fault in {
            SimulatedFault.RATE_LIMIT_429,
            SimulatedFault.IP_BAN_418,
            SimulatedFault.TIMESTAMP_1021,
            SimulatedFault.DISCONNECT,
        }:
            raise InjectedExchangeError(fault)
        if fault is SimulatedFault.REJECTED_STOP and intent.role is OrderRole.STOP:
            rejected = SimulatedOrder(intent=intent, status=SimulatedOrderStatus.REJECTED)
            self._orders[intent.client_order_id] = rejected
            self._economic_keys[intent.economic_key] = intent.client_order_id
            raise InjectedExchangeError(fault)

        order = SimulatedOrder(intent=intent, status=SimulatedOrderStatus.NEW)
        self._orders[intent.client_order_id] = order
        self._economic_keys[intent.economic_key] = intent.client_order_id
        if fault is SimulatedFault.UNKNOWN_503:
            order.status = SimulatedOrderStatus.UNKNOWN
            raise UnknownOrderOutcome("unknown exchange outcome must be reconciled before retry")

        if fault is SimulatedFault.PARTIAL_FILL:
            order.status = SimulatedOrderStatus.PARTIALLY_FILLED
            order.filled_quantity = intent.quantity / Decimal("2")
        self._schedule_event(
            order,
            delayed=fault is SimulatedFault.DELAYED_EVENT,
            duplicate=fault is SimulatedFault.DUPLICATE_EVENT,
        )
        return order

    def resolve_unknown_as_absent(self, client_order_id: str) -> None:
        order = self._require_order(client_order_id)
        if order.status is not SimulatedOrderStatus.UNKNOWN:
            raise SimulatorError("only unknown orders can resolve as absent")
        order.status = SimulatedOrderStatus.CANCELLED
        self._economic_keys.pop(order.intent.economic_key, None)
        self._schedule_event(order)

    def advance_to(self, now_ms: int) -> tuple[SimulatorEvent, ...]:
        if now_ms < self.now_ms:
            raise ValueError("simulation clock cannot move backward")
        self.now_ms = now_ms
        due = tuple(event for event in self._events if event.available_at_ms <= self.now_ms)
        self._events = [event for event in self._events if event.available_at_ms > self.now_ms]
        return due

    def order(self, client_order_id: str) -> SimulatedOrder:
        return self._require_order(client_order_id)

    def _schedule_event(
        self, order: SimulatedOrder, *, delayed: bool = False, duplicate: bool = False
    ) -> None:
        self._event_counter += 1
        event = SimulatorEvent(
            event_id=f"sim-{self._event_counter}",
            client_order_id=order.intent.client_order_id,
            status=order.status,
            filled_quantity=order.filled_quantity,
            available_at_ms=self.now_ms + (1_000 if delayed else 0),
        )
        self._events.append(event)
        if duplicate:
            self._events.append(event)

    def _require_order(self, client_order_id: str) -> SimulatedOrder:
        order = self._orders.get(client_order_id)
        if order is None:
            raise SimulatorError("unknown client order ID")
        return order
